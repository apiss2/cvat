// SPDX-License-Identifier: MIT
import {
    ActionParameterType, BaseCollectionAction, Job, ObjectType, ShapeType, Source,
} from 'cvat-core-wrapper';
import type { CVATCore, ObjectState, MLModel, Task } from 'cvat-core-wrapper';
import { track } from './tracking';
import { MAX_TRACKING_FRAMES } from './selection';
import type { Polygon, TrackerReply } from './tracking';

type Input = Parameters<BaseCollectionAction['run']>[0];
type Output = Awaited<ReturnType<BaseCollectionAction['run']>>;
type Shape = Input['collection']['shapes'][number];
type Track = Input['collection']['tracks'][number];

export interface TrackerDefinition {
    name: string;
    functionID: string;
}

export const SAM2_TRACKER: TrackerDefinition = { name: 'SAM2', functionID: 'pth-sam2-tracker' };
const noChanges = (): Output => ({
    created: { shapes: [], tracks: [], tags: [] },
    deleted: { shapes: [], tracks: [], tags: [] },
});

export class PolygonTrackAction extends BaseCollectionAction {
    private readonly core: CVATCore;
    private instance: Job | null = null;
    private model: MLModel | null = null;
    private span = 50;

    constructor(core: CVATCore, private readonly definition: TrackerDefinition = SAM2_TRACKER) {
        super();
        this.core = core;
    }
    get name(): string { return `${this.definition.name}: track polygon shapes`; }
    get parameters(): NonNullable<BaseCollectionAction['parameters']> {
        return {
            'Frames to track': { type: ActionParameterType.NUMBER, values: ['1', String(MAX_TRACKING_FRAMES), '1'], defaultValue: '50' },
        };
    }
    async init(instance: Job | Task, parameters: Record<string, string | number>): Promise<void> {
        if (!(instance instanceof Job)) throw new Error(`Run ${this.definition.name} tracking inside a 2D annotation job.`);
        if (instance.dimension !== '2d') throw new Error(`${this.definition.name} tracking supports 2D jobs only.`);
        this.instance = instance;
        this.span = Number(parameters['Frames to track']);
        if (!Number.isInteger(this.span) || this.span < 1 || this.span > MAX_TRACKING_FRAMES) {
            throw new Error(`Tracking range must span 1 to ${MAX_TRACKING_FRAMES} frame indices.`);
        }
        const { models } = await this.core.lambda.list();
        this.model = models.find((model) => model.id === this.definition.functionID && model.kind === 'tracker') ?? null;
        if (!this.model) throw new Error(`Deploy the Nuclio function ${this.definition.functionID} first.`);
    }
    async destroy(): Promise<void> {
        // The standard tracker API has no release operation. Redis TTL cleanup
        // bounds abandoned/completed sessions without inventing another CVAT endpoint.
        this.instance = null;
        this.model = null;
    }
    isApplicableForObject(state: ObjectState): boolean {
        return state.objectType === ObjectType.SHAPE && state.shapeType === ShapeType.POLYGON &&
            !state.outside && !state.lock && !state.rotation;
    }
    applyFilter(input: Parameters<BaseCollectionAction['applyFilter']>[0]): Input['collection'] {
        return {
            shapes: input.collection.shapes.filter((shape) => shape.type === ShapeType.POLYGON &&
                shape.frame === input.frameData.number && !shape.outside && !shape.rotation),
            tracks: [], tags: [],
        };
    }
    private async assertSeedsUnchanged(shapes: Shape[], frame: number): Promise<void> {
        const states = await this.instance!.annotations.get(frame, false, []);
        for (const shape of shapes) {
            const current = states.find((state: ObjectState) => state.clientID === shape.clientID);
            if (!current || !this.isApplicableForObject(current)) {
                throw new Error('A seed was removed, locked, or changed. No tracking result has been applied.');
            }
            const exported = await current.export() as Shape;
            for (const key of ['points', 'attributes', 'label_id', 'group', 'frame', 'rotation', 'occluded', 'z_order'] as const) {
                if (JSON.stringify(exported[key]) !== JSON.stringify(shape[key])) {
                    throw new Error('A seed changed during tracking. No tracking result has been applied.');
                }
            }
        }
    }
    async run({ collection, frameData, cancelled, onProgress }: Input): Promise<Output> {
        if (!this.instance || !this.model) throw new Error(`${this.definition.name} action is not initialized.`);
        if (cancelled()) return noChanges();
        const instance = this.instance;
        const model = this.model;
        // Copy the snapshot, never mutate the source collection while requests are running.
        const seeds = collection.shapes.map((shape) => JSON.parse(JSON.stringify(shape)) as Shape);
        await this.assertSeedsUnchanged(seeds, frameData.number);
        const frameNumbers = await instance.frames.frameNumbers();
        const keyframes = await track({
            trackerName: this.definition.name,
            seeds: seeds.map((shape): Polygon => ({ type: 'polygon', points: shape.points ?? [] })),
            start: frameData.number, stop: instance.stopFrame, span: this.span, frameNumbers,
            width: frameData.width, height: frameData.height,
        }, {
            frame: async (frame) => {
                const metadata = await instance.frames.get(frame);
                return { width: metadata.width, height: metadata.height, deleted: metadata.deleted };
            },
            call: async (args) => this.core.lambda.call(instance.taskId, model, {
                ...args, job: instance.id,
            }) as Promise<TrackerReply>,
        }, cancelled, onProgress);
        if (!keyframes || cancelled()) return noChanges();
        await this.assertSeedsUnchanged(seeds, frameData.number);
        if (cancelled()) return noChanges();
        const tracks = seeds.map((seed, index): Track => {
            const label = instance.labels.find((candidate) => candidate.id === seed.label_id);
            if (!label) throw new Error('The seed label no longer exists.');
            const mutableIDs = new Set(label.attributes.filter((attribute) => attribute.mutable).map((attribute) => attribute.id));
            return {
                label_id: seed.label_id, group: seed.group, frame: seed.frame,
                source: Source.SEMI_AUTO, elements: [],
                attributes: seed.attributes.filter((attribute) => !mutableIDs.has(attribute.spec_id)),
                shapes: keyframes[index].map((keyframe) => ({
                    ...keyframe, type: ShapeType.POLYGON, rotation: 0,
                    occluded: seed.occluded, z_order: seed.z_order,
                    attributes: seed.attributes.filter((attribute) => mutableIDs.has(attribute.spec_id)),
                })),
            };
        });
        onProgress(`${this.definition.name} tracking complete`, 100);
        // BaseCollectionAction commits both sides as one undoable annotation action.
        // Saving to the server remains the user's normal Save operation.
        return {
            created: { shapes: [], tags: [], tracks },
            deleted: { shapes: collection.shapes, tags: [], tracks: [] },
        };
    }
}

// Preserve the existing public class and action name for saved workflows and tests.
export class SAM2TrackAction extends PolygonTrackAction {}
