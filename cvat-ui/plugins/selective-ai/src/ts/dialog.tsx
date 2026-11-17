// SPDX-License-Identifier: MIT
import React, { useCallback, useEffect, useRef, useState } from '@modules/react';
import { createRoot } from '@modules/react-dom/client';
import Alert from 'antd/lib/alert';
import Button from 'antd/lib/button';
import Checkbox from 'antd/lib/checkbox';
import Form from 'antd/lib/form';
import InputNumber from 'antd/lib/input-number';
import Modal from 'antd/lib/modal';
import Progress from 'antd/lib/progress';
import Select from 'antd/lib/select';
import Space from 'antd/lib/space';
import Table from 'antd/lib/table';
import {
    buildMapping, buildPlan, executePlan, readLabels,
    type API, type Condition, type Frame, type LabelSpec, type Mapping, type Model, type Plan, type Report, type Scope,
} from './engine';
import { MappingEditor } from './mapper';
import FilterEditor from './filter-editor';
import { supportsThreshold, thresholdParameters } from './model-options';

export interface DialogHandle { focus(): void; dispose(): void }
export interface DialogOptions {
    api: API;
    scope: Scope;
    title: string;
    loadModels(): Promise<Model[]>;
    onClose(): void;
}

export function openDialog(options: DialogOptions): DialogHandle {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    const session = { busy: false, cancelled: false, disposed: false, closed: false };
    const unload = (event: BeforeUnloadEvent): void => {
        if (session.busy) { event.preventDefault(); event.returnValue = ''; }
    };
    window.addEventListener('beforeunload', unload);
    const close = (): void => {
        if (session.busy || session.closed) return;
        session.closed = true;
        window.removeEventListener('beforeunload', unload);
        root.unmount();
        container.remove();
        options.onClose();
    };

    function Dialog(): JSX.Element {
        const [loading, setLoading] = useState(true);
        const [busy, setBusy] = useState(false);
        const [stopping, setStopping] = useState(false);
        const [error, setError] = useState('');
        const [models, setModels] = useState<Model[]>([]);
        const [labels, setLabels] = useState<LabelSpec[]>([]);
        const [modelId, setModelId] = useState<string>();
        const [choices, setChoices] = useState<Mapping>({});
        const [condition, setCondition] = useState<Condition>({ taskFilter: {}, jobFilter: {}, filters: [] });
        const [filterValid, setFilterValid] = useState(false);
        const previousFilters = useRef('');
        const [threshold, setThreshold] = useState<number | null>(0.5);
        const [convert, setConvert] = useState(true);
        const [plan, setPlan] = useState<Plan>();
        const [selected, setSelected] = useState<Set<string>>(new Set());
        const [report, setReport] = useState<Report>();
        const [ran, setRan] = useState(false);
        const model = models.find((item) => String(item.id) === modelId);
        const thresholdEditable = !!model && supportsThreshold(model);
        let mappingValid = false;
        if (model) {
            try { buildMapping(model.labels, labels, choices); mappingValid = true; } catch { /* Show selectors below. */ }
        }

        useEffect(() => {
            let alive = true;
            Promise.all([options.loadModels(), readLabels(options.api, options.scope, () => session.cancelled)])
                .then(([available, targetLabels]) => {
                    if (!alive) return;
                    setModels(available.filter((item) => item.kind === 'detector'));
                    setLabels(targetLabels);
                }).catch((reason) => { if (alive) setError(String(reason)); })
                .finally(() => { if (alive) setLoading(false); });
            return () => { alive = false; session.cancelled = true; };
        }, []);

        const invalidate = (): void => { setPlan(undefined); setSelected(new Set()); setReport(undefined); setRan(false); };
        const changeFilters = useCallback((filters: object[], valid: boolean, taskFilter: object, jobFilter: object): void => {
            const next = { filters, taskFilter, jobFilter };
            const signature = JSON.stringify([next, valid]);
            if (previousFilters.current === signature) return;
            previousFilters.current = signature;
            setCondition(next); setFilterValid(valid);
            setPlan(undefined); setSelected(new Set()); setReport(undefined); setRan(false); setError('');
        }, []);
        const start = (): void => {
            session.busy = true; session.cancelled = false;
            setBusy(true); setStopping(false); setError('');
        };
        const finish = (): void => {
            session.busy = false;
            if (session.disposed) close(); else setBusy(false);
        };
        const preview = async (): Promise<void> => {
            if (session.busy || session.disposed || !filterValid || loading) return;
            start(); invalidate();
            try {
                const next = await buildPlan(options.api, options.scope, condition, () => session.cancelled);
                if (!session.disposed) {
                    setPlan(next); setSelected(new Set(next.frames.map((frame) => frame.key)));
                }
            } catch (reason) { if (!session.disposed) setError(String(reason)); } finally { finish(); }
        };
        const run = async (): Promise<void> => {
            if (!plan || !model || session.disposed || session.busy || !filterValid || !mappingValid ||
                (thresholdEditable && threshold === null)) return;
            start(); setRan(true); setReport({ outcomes: [], cancelled: false });
            try {
                await executePlan(options.api, plan, new Set(selected), model, { ...choices }, threshold, convert,
                    () => session.cancelled,
                    (next) => { if (!session.disposed) setReport(next); });
            } finally { finish(); }
        };
        const selectModel = (id: string): void => {
            setModelId(id);
            // Remount MappingEditor by model ID and discard the previous model's mapping.
            setChoices({});
        };
        const downloadReport = (): void => {
            const body = JSON.stringify({
                scope: options.scope, condition: plan?.condition, model: model?.id,
                selected: [...selected], mapping: choices, ...(model ? thresholdParameters(model, threshold) : {}),
                conv_mask_to_poly: convert, report,
            }, null, 2);
            const url = URL.createObjectURL(new Blob([body], { type: 'application/json' }));
            const link = document.createElement('a');
            link.href = url; link.download = `selective-ai-${options.scope.type}-${options.scope.id}.json`;
            link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        };
        const done = report?.outcomes.length ?? 0;
        const statusNames = { saved: '保存済み', empty: '出力なし', skipped: '対象から除外', failed: '失敗または結果不明' };
        const statuses = new Map(report?.outcomes.map((outcome) => [`${outcome.taskId}:${outcome.frame}`, outcome.status]) ?? []);
        return (
            <Modal
                open title={`条件を指定して自動アノテーション: ${options.title}`} width={1000}
                style={{ top: 12, maxWidth: 'calc(100vw - 24px)', paddingBottom: 0 }}
                styles={{
                    content: { display: 'flex', flexDirection: 'column', maxHeight: 'calc(100dvh - 24px)' },
                    header: { flexShrink: 0, overflowWrap: 'anywhere' },
                    body: { flex: '1 1 auto', minHeight: 0, minWidth: 0, overflow: 'auto' },
                    footer: { flexShrink: 0 },
                }}
                onCancel={close} closable={!busy} maskClosable={false} keyboard={!busy}
                footer={(
                    <Space wrap>
                        <Button onClick={() => { void preview(); }} disabled={busy || loading ||
                            !filterValid || ran}>対象画像を一覧表示</Button>
                        <Button type='primary' onClick={() => { void run(); }} disabled={busy || loading || !plan ||
                            !selected.size || !filterValid || !mappingValid || (thresholdEditable && threshold === null) || ran}>選択した画像に実行</Button>
                        {busy && <Button danger disabled={stopping} onClick={() => {
                            session.cancelled = true; setStopping(true);
                        }}>{stopping ? '中止中' : '中止'}</Button>}
                        {ran && <Button disabled={busy} onClick={invalidate}>条件を変更</Button>}
                        <Button disabled={busy} onClick={close}>閉じる</Button>
                        {report && <Button disabled={busy} onClick={downloadReport}>結果を保存</Button>}
                    </Space>
                )}
            >
                <Space direction='vertical' style={{ width: '100%' }} size={12}>
                    <Alert type='warning' showIcon
                        message='結果は追記されます。実行中はタブを閉じず、対象タスクの編集を避けてください。再実行で重複する場合があります。' />
                    {error && <Alert type='error' showIcon message={error} />}
                    {!loading && !models.length && <Alert type='info' message='利用できる推論モデルがありません。' />}
                    {!loading && <FilterEditor labels={labels} disabled={busy || ran} onChange={changeFilters} />}
                    <Form layout='vertical' disabled={busy || loading || ran}>
                        <Form.Item label='推論モデル'>
                            <Select value={modelId} onChange={selectModel} placeholder='モデルを選択'
                                options={models.map((item) => ({ value: String(item.id), label: item.name }))} />
                        </Form.Item>
                        {model && <Form.Item label='出力ラベルと属性の対応'>
                            <MappingEditor key={String(model.id)} model={model} labels={labels}
                                disabled={busy || loading || ran} onChange={setChoices} />
                        </Form.Item>}
                        {thresholdEditable && <Form.Item label='信頼度の閾値'>
                            <InputNumber<number> value={threshold} min={0} max={1} step={0.05} onChange={setThreshold} />
                        </Form.Item>}
                        <Checkbox checked={convert} onChange={(event) => setConvert(event.target.checked)}>マスクをポリゴンに変換</Checkbox>
                    </Form>
                    {plan && <>
                        <div>{`${plan.tasks.length}タスク / 候補${plan.frames.length}画像 / 選択${selected.size}画像`}</div>
                        <Space>
                            <Button disabled={busy || ran} onClick={() => setSelected(new Set(plan.frames.map((frame) => frame.key)))}>全選択</Button>
                            <Button disabled={busy || ran} onClick={() => setSelected(new Set())}>全解除</Button>
                        </Space>
                        <Table<Frame> size='small' rowKey='key' dataSource={plan.frames} scroll={{ x: 560 }}
                            pagination={{ pageSize: 20, showSizeChanger: false }}
                            rowSelection={{ selectedRowKeys: [...selected], preserveSelectedRowKeys: true,
                                onChange: (keys) => setSelected(new Set(keys.map(String))),
                                getCheckboxProps: () => ({ disabled: busy || ran }),
                            }} columns={[
                                { title: 'タスク', dataIndex: 'taskId', width: 90 },
                                { title: 'フレーム番号（0始まり）', dataIndex: 'frame', width: 150 },
                                { title: '画像名', dataIndex: 'name', ellipsis: true },
                                { title: '結果', key: 'result', render: (_: unknown, frame: Frame) => {
                                    const status = statuses.get(frame.key); return status ? statusNames[status] : '';
                                } },
                            ]} />
                    </>}
                    {report && <>
                        <Progress percent={selected.size ? Math.floor(done * 100 / selected.size) : 0}
                            status={report.error ? 'exception' : busy ? 'active' : 'normal'} />
                        <div>{`保存${report.outcomes.filter((item) => item.status === 'saved').length} / 出力なし${report.outcomes.filter((item) => item.status === 'empty').length} / 除外${report.outcomes.filter((item) => item.status === 'skipped').length}`}</div>
                        {report.error && <Alert type='error' showIcon message={report.error} />}
                        {report.cancelled && <Alert type='warning' message='処理を中止しました。保存済みの結果は残っています。' />}
                        {!busy && !report.error && !report.cancelled && <Alert type='success' message='選択画像の処理が完了しました。' />}
                    </>}
                </Space>
            </Modal>
        );
    }
    root.render(<Dialog />);
    return {
        focus: () => { window.focus(); },
        dispose: () => { session.disposed = true; session.cancelled = true; close(); },
    };
}
