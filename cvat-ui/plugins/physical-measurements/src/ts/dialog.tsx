// SPDX-License-Identifier: MIT
import React, { useEffect, useRef, useState } from '@modules/react';
import { createRoot } from '@modules/react-dom/client';
import Alert from 'antd/lib/alert';
import Button from 'antd/lib/button';
import Col from 'antd/lib/col';
import Divider from 'antd/lib/divider';
import Form from 'antd/lib/form';
import InputNumber from 'antd/lib/input-number';
import Modal from 'antd/lib/modal';
import Radio from 'antd/lib/radio';
import Row from 'antd/lib/row';
import Space from 'antd/lib/space';
import Spin from 'antd/lib/spin';
import Switch from 'antd/lib/switch';
import Tag from 'antd/lib/tag';
import { errorMessage, type MeasurementAPI, type MeasurementDocument } from './api';
import { parseMeasurementConfig, type MeasurementConfig, type PixelSpacing } from './measurement';

export interface DialogHandle { focus(): void; dispose(): void }

interface DialogOptions {
    title: string;
    frame: number;
    onSaved(document: MeasurementDocument): void;
}

type SpacingDraft = Omit<PixelSpacing, 'x' | 'y'> & { x: number | null; y: number | null };
interface ConfigDraft {
    version: 1;
    default: SpacingDraft | null;
    frames: Record<string, SpacingDraft | null>;
}

type FrameMode = 'inherit' | 'custom' | 'pixels';
const MAX_IMPORT_BYTES = 1024 * 1024;
const emptySpacing = (): SpacingDraft => ({ x: null, y: null, unit: 'mm' });

function copyConfig(config: MeasurementConfig): ConfigDraft {
    return {
        version: 1,
        default: config.default ? { ...config.default } : null,
        frames: Object.fromEntries(Object.entries(config.frames).map(([frame, spacing]) => (
            [frame, spacing ? { ...spacing } : null]
        ))),
    };
}

function checkedConfig(draft: unknown, frameCount: number): MeasurementConfig {
    const config = parseMeasurementConfig(draft);
    if (Object.keys(config.frames).some((frame) => Number(frame) >= frameCount)) {
        throw new Error('タスクに存在しないフレーム番号があります。');
    }
    return config;
}

function SpacingFields({
    value, disabled, name, onChange,
}: {
    value: SpacingDraft;
    disabled: boolean;
    name: string;
    onChange(value: SpacingDraft): void;
}): JSX.Element {
    return (
        <Row gutter={12}>
            {(['x', 'y'] as const).map((axis) => (
                <Col span={12} key={axis}>
                    <Form.Item label={axis.toUpperCase()} htmlFor={`cvat-spacing-${name}-${axis}`}>
                        <InputNumber<number> id={`cvat-spacing-${name}-${axis}`}
                            aria-label={`${name === 'default' ? 'タスク共通' : 'フレーム'} ${axis.toUpperCase()} mm/px`}
                            value={value[axis]} disabled={disabled} min={0} step={0.01}
                            status={value[axis] !== null && (!Number.isFinite(value[axis]) || value[axis]! <= 0) ? 'error' : ''}
                            addonAfter='mm/px' style={{ width: '100%' }}
                            onChange={(next) => onChange({ ...value, [axis]: next })} />
                    </Form.Item>
                </Col>
            ))}
        </Row>
    );
}

export function openDialog(api: MeasurementAPI, options: DialogOptions, onClose: () => void): DialogHandle {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    const session = { busy: false, disposed: false, confirmation: null as { destroy(): void } | null };
    const dispose = (): void => {
        if (session.disposed) return;
        session.disposed = true;
        session.confirmation?.destroy();
        api.dispose();
        queueMicrotask(() => { root.unmount(); container.remove(); onClose(); });
    };

    function Dialog(): JSX.Element {
        const [saved, setSaved] = useState<MeasurementDocument>();
        const [draft, setDraft] = useState<ConfigDraft>({ version: 1, default: null, frames: {} });
        const [frame, setFrame] = useState(
            Number.isSafeInteger(options.frame) && options.frame >= 0 ? options.frame : 0,
        );
        const [loading, setLoading] = useState(true);
        const [saving, setSaving] = useState(false);
        const [importing, setImporting] = useState(false);
        const [error, setError] = useState('');
        const [savedNotice, setSavedNotice] = useState(false);
        const input = useRef<HTMLInputElement>(null);
        const rememberedDefault = useRef<SpacingDraft | null>(null);
        const busy = loading || saving || importing;
        const editable = !!saved?.supported && !saved.readOnly && !busy;
        const dirty = !!saved && JSON.stringify(draft) !== JSON.stringify(copyConfig(saved));
        const frameKey = String(frame);
        const hasOverride = Object.prototype.hasOwnProperty.call(draft.frames, frameKey);
        let frameMode: FrameMode = 'inherit';
        if (hasOverride) frameMode = draft.frames[frameKey] === null ? 'pixels' : 'custom';
        const frameSpacing = hasOverride ? draft.frames[frameKey] : draft.default;

        const load = async (): Promise<void> => {
            if (session.busy || session.disposed) return;
            session.busy = true; setLoading(true); setError(''); setSavedNotice(false);
            try {
                const document = await api.get();
                if (session.disposed) return;
                setSaved(document);
                setDraft(copyConfig(document));
                rememberedDefault.current = document.default ? { ...document.default } : null;
                setFrame((current) => Math.min(current, Math.max(0, document.frame_count - 1)));
            } catch (reason) {
                if (!session.disposed) setError(errorMessage(reason));
            } finally {
                session.busy = false;
                if (!session.disposed) setLoading(false);
            }
        };
        useEffect(() => { load(); }, []);

        const confirmDiscard = (action: () => void): void => {
            if (session.busy || session.disposed || session.confirmation) return;
            if (!dirty) { action(); return; }
            session.confirmation = Modal.confirm({
                title: '未保存の変更を破棄しますか？',
                okText: '破棄',
                cancelText: '戻る',
                okButtonProps: { danger: true },
                onOk: () => { session.confirmation = null; action(); },
                onCancel: () => { session.confirmation = null; },
            });
        };
        const change = (next: ConfigDraft): void => {
            if (!editable || session.disposed) return;
            setDraft(next); setSavedNotice(false);
        };
        const setFrameMode = (mode: FrameMode): void => {
            const frames = { ...draft.frames };
            if (mode === 'inherit') delete frames[frameKey];
            else {
                frames[frameKey] = mode === 'pixels' ? null : {
                    ...(draft.frames[frameKey] || draft.default || emptySpacing()),
                };
            }
            change({ ...draft, frames });
        };
        const save = async (): Promise<void> => {
            if (!editable || !saved || session.busy || session.disposed) return;
            session.busy = true; setSaving(true); setError(''); setSavedNotice(false);
            let result: MeasurementDocument | undefined;
            try {
                const config = checkedConfig(draft, saved.frame_count);
                result = await api.save(config, saved.revision);
                if (session.disposed) return;
                setSaved(result); setDraft(copyConfig(result)); setSavedNotice(true);
                rememberedDefault.current = result.default ? { ...result.default } : null;
            } catch (reason) {
                if (!session.disposed) setError(errorMessage(reason));
            } finally {
                session.busy = false;
                if (!session.disposed) setSaving(false);
            }
            if (result && !session.disposed) options.onSaved(result);
        };
        const importFile = async (file: File): Promise<void> => {
            if (!editable || !saved || session.busy || session.disposed) return;
            session.busy = true; setImporting(true); setError(''); setSavedNotice(false);
            try {
                if (file.size > MAX_IMPORT_BYTES) throw new Error('JSONファイルは1 MiB以下にしてください。');
                const text = await file.text();
                if (session.disposed) return;
                const config = checkedConfig(JSON.parse(text), saved.frame_count);
                setDraft(copyConfig(config));
                rememberedDefault.current = config.default ? { ...config.default } : null;
            } catch (reason) {
                if (!session.disposed) setError(errorMessage(reason));
            } finally {
                session.busy = false;
                if (!session.disposed) setImporting(false);
            }
        };
        const exportFile = (): void => {
            if (!saved || saved.readOnly || busy || session.disposed) return;
            try {
                const config = checkedConfig(draft, saved.frame_count);
                const url = URL.createObjectURL(new Blob([`${JSON.stringify(config, null, 2)}\n`], {
                    type: 'application/json',
                }));
                const link = document.createElement('a');
                link.href = url; link.download = 'physical-measurements.json';
                document.body.appendChild(link); link.click(); link.remove();
                window.setTimeout(() => URL.revokeObjectURL(url), 0);
            } catch (reason) {
                setError(errorMessage(reason));
            }
        };

        return (
            <Modal open wrapClassName='cvat-physical-measurements-modal' title={`物理長: ${options.title}`} width={620}
                onCancel={() => confirmDiscard(dispose)} closable={!busy} maskClosable={false} keyboard={!busy}
                footer={(
                    <Row justify='space-between' gutter={[8, 8]}>
                        <Col><Space>
                            <Button disabled={!editable} loading={importing}
                                onClick={() => input.current?.click()}>JSON読込</Button>
                            <Button disabled={!saved || saved.readOnly || busy} onClick={exportFile}>JSON出力</Button>
                        </Space></Col>
                        <Col><Space>
                            <Button disabled={busy} onClick={() => confirmDiscard(() => { load(); })}>再読込</Button>
                            <Button disabled={busy} onClick={() => confirmDiscard(dispose)}>閉じる</Button>
                            <Button type='primary' disabled={!editable || !dirty} loading={saving}
                                onClick={() => { save(); }}>保存</Button>
                        </Space></Col>
                    </Row>
                )}
            >
                <input ref={input} type='file' accept='application/json,.json' hidden disabled={!editable}
                    onChange={(event) => {
                        const { target } = event;
                        const file = target.files?.[0];
                        target.value = '';
                        if (file) importFile(file);
                    }} />
                <Space direction='vertical' style={{ width: '100%' }}>
                    {error && <Alert type='error' message={error} showIcon />}
                    {savedNotice && <Alert type='success' message='保存しました。' showIcon />}
                    {saved?.readOnly && <Tag>閲覧のみ</Tag>}
                    {saved && !saved.supported && (
                        <Alert type='info' message={saved.unavailable_reason || 'このタスクでは物理長を表示できません。'} showIcon />
                    )}
                    <Spin spinning={loading}>
                        <Form layout='vertical'>
                            <Form.Item label='タスク共通'>
                                <Space>
                                    <Switch checked={draft.default !== null} disabled={!editable}
                                        aria-label='タスク共通のspacingを使用'
                                        onChange={(enabled) => {
                                            if (!enabled) rememberedDefault.current = draft.default;
                                            change({
                                                ...draft,
                                                default: enabled ?
                                                    rememberedDefault.current || emptySpacing() : null,
                                            });
                                        }} />
                                    <Tag>{draft.default ? 'mm' : 'px'}</Tag>
                                    {dirty && <Tag color='orange'>未保存</Tag>}
                                </Space>
                            </Form.Item>
                            {draft.default && <SpacingFields name='default' value={draft.default} disabled={!editable}
                                onChange={(value) => change({ ...draft, default: value })} />}
                            <Divider style={{ margin: '8px 0 20px' }} />
                            <Form.Item label='フレーム' htmlFor='cvat-spacing-frame-number'>
                                <Space>
                                    <InputNumber<number> id='cvat-spacing-frame-number' aria-label='フレーム番号'
                                        value={frame} min={0} max={Math.max(0, (saved?.frame_count || 0) - 1)}
                                        precision={0}
                                        disabled={!saved || saved.readOnly || busy || !saved.frame_count} addonBefore='#'
                                        onChange={(value) => {
                                            if (value !== null && Number.isSafeInteger(value) && value >= 0 &&
                                                value < (saved?.frame_count || 0)) setFrame(value);
                                        }} />
                                    <Tag>{frameSpacing ? 'mm' : 'px'}</Tag>
                                </Space>
                            </Form.Item>
                            <Form.Item label='適用する設定'>
                                <Radio.Group value={frameMode} disabled={!editable || !saved?.frame_count}
                                    onChange={(event) => setFrameMode(event.target.value as FrameMode)}>
                                    <Radio.Button value='inherit'>タスク共通</Radio.Button>
                                    <Radio.Button value='custom'>個別</Radio.Button>
                                    <Radio.Button value='pixels'>px</Radio.Button>
                                </Radio.Group>
                            </Form.Item>
                            {frameMode === 'custom' && frameSpacing && (
                                <SpacingFields name='frame' value={frameSpacing} disabled={!editable}
                                    onChange={(value) => change({
                                        ...draft, frames: { ...draft.frames, [frameKey]: value },
                                    })} />
                            )}
                        </Form>
                    </Spin>
                </Space>
            </Modal>
        );
    }

    root.render(<Dialog />);
    return {
        focus: () => {
            const dialog = container.querySelector<HTMLElement>('.cvat-physical-measurements-modal') ||
                document.querySelector<HTMLElement>('.cvat-physical-measurements-modal');
            dialog?.focus();
        },
        dispose,
    };
}
