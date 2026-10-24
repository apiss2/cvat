// SPDX-License-Identifier: MIT
import type { ComponentBuilder, PluginEntryPoint } from 'components/plugins-entrypoint';
import { createDisplay, MeasurementCanvas } from './display';

const builder: ComponentBuilder = ({ store }) => {
    const display = createDisplay((error) => { console.error('Could not read DicomTag PixelSpacing', error); });
    const jobOwners = new WeakMap<object, string>();
    let disposed = false;
    const sync = (): void => {
        if (disposed) return;
        const state = store.getState();
        const {
            job, canvas, annotations, player,
        } = state.annotation;
        const { instance } = job;
        const { user } = state.auth;
        const owner = user ? JSON.stringify([user.id, state.organizations.current?.id ?? null,
            state.organizations.current?.slug ?? '']) : '';
        // A context switch may leave the old Job in Redux until navigation finishes.
        // Its cached annotations must not be reused under another user/organization.
        if (instance && owner) {
            const previousOwner = jobOwners.get(instance);
            if (previousOwner && previousOwner !== owner) {
                display.update(null);
                return;
            }
            jobOwners.set(instance, owner);
        }
        if (!user || !instance || instance.dimension !== '2d' || job.fetching ||
            !canvas.instance || !canvas.ready || !annotations.initialized || player.frame.fetching) {
            display.update(null);
            return;
        }

        display.update({
            key: `${owner}:${instance.id}`,
            canvas: canvas.instance as MeasurementCanvas,
            annotations: instance.annotations,
            frame: player.frame.number,
            // Edits, removal, undo/redo and filtered refreshes replace this array.
            // Read the unfiltered collection rather than using these visible states as metadata.
            revision: annotations.states,
        });
    };
    sync();
    return {
        name: 'Physical polyline measurements',
        globalStateDidUpdate: sync,
        destructor: (): void => {
            disposed = true;
            display.dispose();
        },
    };
};

let registered = false;
function register(): void {
    const host = (window as unknown as { cvatUI?: { registerComponent: PluginEntryPoint } }).cvatUI;
    if (host && !registered) { registered = true; host.registerComponent(builder); }
}
window.addEventListener('plugins.ready', register, { once: true });
register();
