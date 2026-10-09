// SPDX-License-Identifier: MIT
import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import Alert from 'antd/lib/alert';
import Button from 'antd/lib/button';
import Checkbox from 'antd/lib/checkbox';
import InputNumber from 'antd/lib/input-number';
import Modal from 'antd/lib/modal';
import Progress from 'antd/lib/progress';
import Space from 'antd/lib/space';
import type { MenuProps } from 'antd/lib/menu';
import type { ComponentBuilder, ComponentBuilderArgs } from 'components/plugins-entrypoint';
import type { Job, ObjectState } from 'cvat-core-wrapper';
import { Canvas } from 'cvat-canvas-wrapper';
import { fetchAnnotationsAsync } from 'actions/annotation-actions';
import { PolygonTrackAction } from './action';
import type { TrackerDefinition } from './action';
import { MAX_TRACKING_OBJECTS, MAX_TRACKING_FRAMES, resolveSelection, validateRange } from './selection';

type Host = ComponentBuilderArgs;
type Props = { host: Host; job: Job; definition: TrackerDefinition; onClose: () => void };

function TrackingDialog({ host, job, definition, onClose }: Props): JSX.Element {
    const initialFrame = host.store.getState().annotation.player.frame.number;
    const [start, setStart] = useState(initialFrame);
    const [end, setEnd] = useState(Math.min(initialFrame + 50, job.stopFrame));
    const [candidates, setCandidates] = useState<ObjectState[]>([]);
    const [loadedStart, setLoadedStart] = useState<number | null>(null);
    const [ids, setIDs] = useState<number[]>([]);
    const [loading, setLoading] = useState(false);
    const [busy, setBusy] = useState(false);
    const [cancelling, setCancelling] = useState(false);
    const [error, setError] = useState('');
    const [progress, setProgress] = useState(0);
    const [status, setStatus] = useState('');
    const [dimensions, setDimensions] = useState({ width: 1, height: 1 });
    const cancelled = useRef(false);
    const alive = useRef(true);
    const rangeError = validateRange(start, end, job.startFrame, job.stopFrame);
    const sameJob = (): boolean => host.store.getState().annotation.job.instance === job;

    useEffect(() => {
        alive.current = true;
        const unsubscribe = host.store.subscribe(() => {
            if (!sameJob()) cancelled.current = true;
        });
        return () => { alive.current = false; cancelled.current = true; unsubscribe(); };
    }, []);

    useEffect(() => {
        // Ignore late responses for a previously selected start frame.
        let obsolete = false;
        setCandidates([]); setIDs([]); setLoadedStart(null); setError('');
        if (!Number.isInteger(start) || start < job.startFrame || start > job.stopFrame) return undefined;
        setLoading(true);
        const action = new PolygonTrackAction(host.core, definition);
        Promise.all([job.frames.get(start), job.annotations.get(start, false, [])]).then(([frame, states]) => {
            if (obsolete || !alive.current) return;
            if (frame.deleted) throw new Error('開始フレームは削除されています。別のフレームを選択してください。');
            setDimensions({ width: frame.width, height: frame.height });
            setCandidates(states.filter((state: ObjectState) => action.isApplicableForObject(state)));
            setLoadedStart(start);
        }).catch((cause: unknown) => {
            if (!obsolete && alive.current) setError(cause instanceof Error ? cause.message : String(cause));
        }).finally(() => { if (!obsolete && alive.current) setLoading(false); });
        return () => { obsolete = true; };
    }, [start]);

    const run = async (): Promise<void> => {
        if (busy || loading || loadedStart !== start) return;
        const invalid = validateRange(start, end, job.startFrame, job.stopFrame);
        if (invalid) { setError(invalid); return; }
        if (!sameJob()) { setError('ジョブが変更されました。ダイアログを閉じて開き直してください。'); return; }
        const action = new PolygonTrackAction(host.core, definition);
        cancelled.current = false;
        setBusy(true); setError(''); setStatus('追跡を初期化しています。'); setProgress(0);
        try {
            const seeds = resolveSelection(candidates, ids);
            await host.core.actions.call(
                job, action, { 'Frames to track': String(end - start) }, start, seeds,
                (text: string, percent: number) => {
                    if (alive.current) { setStatus(text); setProgress(percent); }
                },
                () => cancelled.current || !sameJob(),
            );
            if (alive.current && sameJob()) {
                if (cancelled.current) setStatus('中止しました。アノテーションは変更していません。');
                else {
                    const state = host.store.getState();
                    const canvas = state.annotation.canvas.instance as Canvas;
                    canvas.setup(state.annotation.player.frame.data, []);
                    host.store.dispatch(fetchAnnotationsAsync());
                    setStatus('追跡結果を反映しました。保存前に確認してください。');
                    setCandidates([]); setIDs([]);
                }
            }
        } catch (cause: unknown) {
            if (alive.current) setError(cause instanceof Error ? cause.message : String(cause));
        } finally {
            if (alive.current) { setBusy(false); setCancelling(false); }
        }
    };

    return (
        <Modal
            open title={`${definition.name}: ポリゴンの追跡`} width={760}
            maskClosable={false} closable={!busy} keyboard={!busy}
            onCancel={onClose}
            footer={(
                <Space>
                    <Button onClick={() => {
                        if (busy) { cancelled.current = true; setCancelling(true); }
                        else onClose();
                    }} loading={cancelling}>{busy ? '追跡を中止' : '閉じる'}</Button>
                    <Button type='primary' loading={busy}
                        disabled={loading || busy || loadedStart !== start || !ids.length || Boolean(rangeError)}
                        onClick={() => { void run(); }}
                    >選択したポリゴンを追跡</Button>
                </Space>
            )}
        >
            <Space direction='vertical' size='middle' style={{ width: '100%' }}>
                <Alert type='info' showIcon message={`開始フレームのポリゴンを初期値にして、終了フレームまで前方向に追跡します。終了フレームも含みます。最大${MAX_TRACKING_OBJECTS}個、開始から${MAX_TRACKING_FRAMES}フレーム先まで指定できます。メモリ量と結果サイズにも上限があります。`} />
                <Space wrap>
                    <label htmlFor='polygon-tracking-start'>開始フレーム（初期値）</label>
                    <InputNumber id='polygon-tracking-start' value={start} min={job.startFrame} max={job.stopFrame}
                        precision={0} disabled={busy} onChange={(value) => {
                            if (typeof value === 'number') {
                                setStart(value);
                                if (end <= value) setEnd(Math.min(value + 50, job.stopFrame));
                            }
                        }}
                    />
                    <label htmlFor='polygon-tracking-end'>終了フレーム（含む）</label>
                    <InputNumber id='polygon-tracking-end' value={end} min={job.startFrame} max={Math.min(job.stopFrame, start + MAX_TRACKING_FRAMES)}
                        precision={0} disabled={busy} onChange={(value) => {
                            if (typeof value === 'number') { setEnd(value); setError(''); }
                        }}
                    />
                    <Button disabled={busy || loading} onClick={() => {
                        host.store.dispatch(host.actionCreators.changeFrameAsync(start));
                    }}>開始フレームを表示</Button>
                </Space>
                {rangeError ? <Alert type='warning' message={rangeError} /> : null}
                <div>初期ポリゴンを選択（{ids.length}/{MAX_TRACKING_OBJECTS}個）</div>
                {loading ? <div>開始フレームのポリゴンを読み込んでいます。</div> : null}
                {!loading && !candidates.length ? <div>対象のポリゴンがありません。開始フレームにロックされていないポリゴン図形を描いてください。</div> : null}
                <Space direction='vertical' style={{ width: '100%', maxHeight: 280, overflow: 'auto' }}>
                    {candidates.map((state) => (
                        <Checkbox key={state.clientID} checked={ids.includes(state.clientID)}
                            disabled={busy || (!ids.includes(state.clientID) && ids.length >= MAX_TRACKING_OBJECTS)}
                            onChange={(event) => {
                                setError('');
                                setIDs(event.target.checked ? [...ids, state.clientID] : ids.filter((id) => id !== state.clientID));
                            }}
                        >
                            <Space>
                                <svg width={80} height={60} viewBox={`0 0 ${dimensions.width} ${dimensions.height}`}
                                    role='img' aria-label={`ポリゴン ${state.clientID} の形状`}
                                >
                                    <polygon points={state.points.join(' ')} fill='none' stroke='currentColor'
                                        strokeWidth={Math.max(dimensions.width, dimensions.height) / 100}
                                    />
                                </svg>
                                <span>{`#${state.clientID} / ${state.label.name} / ${state.points.length / 2}頂点`}</span>
                            </Space>
                        </Checkbox>
                    ))}
                </Space>
                <Alert type='warning' showIcon message={`初期フレーム ${start} → 終了フレーム ${end}。選択した${ids.length}個の図形だけをトラックへ置換します。既存のトラックは対象外です。削除されたフレームは処理しません。結果はUndoで一括取消できます。`} />
                {busy ? <Progress percent={progress} /> : null}
                {status ? <div role='status'>{status}</div> : null}
                {error ? <Alert type='error' showIcon message={error} /> : null}
            </Space>
        </Modal>
    );
}

export function trackingPlugin(definition: TrackerDefinition): ComponentBuilder {
    return (host) => {
        const action = new PolygonTrackAction(host.core, definition);
        const registration = host.core.actions.register(action);
        void registration.catch((error: unknown) => console.error('Tracking action registration failed', error));
        let closeDialog: (() => void) | null = null;
        const menu = ({ targetProps }: { targetProps: { jobInstance: Job } }): NonNullable<MenuProps['items']>[0] => ({
            key: `polygon-tracking-${definition.functionID}`,
            label: `${definition.name}: ポリゴンを追跡…`,
            disabled: targetProps.jobInstance.dimension !== '2d',
            onClick: () => {
                if (closeDialog) return;
                const element = document.createElement('div');
                document.body.append(element);
                const root = createRoot(element);
                closeDialog = () => { root.unmount(); element.remove(); closeDialog = null; };
                root.render(<TrackingDialog host={host} job={targetProps.jobInstance} definition={definition}
                    onClose={() => closeDialog?.()}
                />);
            },
        });
        // The annotation-menu extension point consumes Ant Design menu-item factories,
        // despite the common plugin registry typing its entries as React components.
        const component = menu as unknown as React.ComponentType;
        host.dispatch(host.actionCreators.addUIComponent('annotationPage.menuActions.items', component, { weight: 45 }));
        return {
            name: `${definition.name} tracking`,
            destructor: async () => {
                closeDialog?.();
                host.dispatch(host.actionCreators.removeUIComponent('annotationPage.menuActions.items', component));
                await registration;
                await host.core.actions.unregister(action);
                await action.destroy();
            },
        };
    };
}
