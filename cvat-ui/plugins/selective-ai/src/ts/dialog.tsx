// SPDX-License-Identifier: MIT
import React, { useEffect, useState } from '@modules/react';
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
    buildMapping, buildPlan, compatible, executePlan, readLabels,
    type API, type Condition, type Frame, type LabelSpec, type Model, type Plan, type Report, type Scope,
} from './engine';

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
        const [choices, setChoices] = useState<Record<string, string>>({});
        const [condition, setCondition] = useState<Condition>({ mode: 'any', labelIds: [] });
        const [threshold, setThreshold] = useState<number | null>(0.5);
        const [convert, setConvert] = useState(false);
        const [plan, setPlan] = useState<Plan>();
        const [selected, setSelected] = useState<Set<string>>(new Set());
        const [report, setReport] = useState<Report>();
        const [ran, setRan] = useState(false);
        const model = models.find((item) => String(item.id) === modelId);
        let mappingValid = false;
        if (model) {
            try { buildMapping(model.labels, labels, choices); mappingValid = true; } catch { /* Show selectors below. */ }
        }

        useEffect(() => {
            let alive = true;
            Promise.all([options.loadModels(), readLabels(options.api, options.scope)])
                .then(([available, targetLabels]) => {
                    if (!alive) return;
                    setModels(available.filter((item) => item.kind === 'detector'));
                    setLabels(targetLabels);
                }).catch((reason) => { if (alive) setError(String(reason)); })
                .finally(() => { if (alive) setLoading(false); });
            return () => { alive = false; session.cancelled = true; };
        }, []);

        const invalidate = (): void => { setPlan(undefined); setSelected(new Set()); setReport(undefined); setRan(false); };
        const start = (): void => {
            session.busy = true; session.cancelled = false;
            setBusy(true); setStopping(false); setError('');
        };
        const finish = (): void => {
            session.busy = false;
            if (session.disposed) close(); else setBusy(false);
        };
        const preview = async (): Promise<void> => {
            if (session.busy || session.disposed) return;
            start(); invalidate();
            try {
                const next = await buildPlan(options.api, options.scope, condition, () => session.cancelled);
                if (!session.disposed) {
                    setPlan(next); setSelected(new Set(next.frames.map((frame) => frame.key)));
                }
            } catch (reason) { if (!session.disposed) setError(String(reason)); } finally { finish(); }
        };
        const run = async (): Promise<void> => {
            if (!plan || !model || threshold === null || session.busy) return;
            start(); setRan(true); setReport({ outcomes: [], cancelled: false });
            try {
                await executePlan(options.api, plan, new Set(selected), model, { ...choices }, threshold, convert,
                    () => session.cancelled,
                    (next) => { if (!session.disposed) setReport(next); });
            } finally { finish(); }
        };
        const selectModel = (id: string): void => {
            setModelId(id);
            const next = models.find((item) => String(item.id) === id);
            setChoices(Object.fromEntries((next?.labels ?? [])
                .filter((item) => labels.some((label) => label.name === item.name && compatible(item, label)))
                .map((item) => [item.name, item.name])));
        };
        const downloadReport = (): void => {
            const body = JSON.stringify({
                scope: options.scope, condition: plan?.condition, model: model?.id,
                selected: [...selected], threshold, conv_mask_to_poly: convert, report,
            }, null, 2);
            const url = URL.createObjectURL(new Blob([body], { type: 'application/json' }));
            const link = document.createElement('a');
            link.href = url; link.download = `selective-ai-${options.scope.type}-${options.scope.id}.json`;
            link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        };
        const done = report?.outcomes.length ?? 0;
        const statusNames = { saved: '保存済み', empty: '検出なし', skipped: '対象から除外', failed: '失敗または結果不明' };
        const statuses = new Map(report?.outcomes.map((outcome) => [`${outcome.taskId}:${outcome.frame}`, outcome.status]) ?? []);
        return (
            <Modal
                open title={`タグ条件で自動アノテーション: ${options.title}`} width={1000}
                onCancel={close} closable={!busy} maskClosable={!busy} keyboard={!busy} footer={null}
            >
                <Space direction='vertical' style={{ width: '100%' }} size='middle'>
                    <Alert type='warning' showIcon message='保存済みのタグで対象を選び、推論結果を追記します。'
                        description='先にアノテーションを保存してください。実行中はこのタブを開いたままにし、対象タスクの編集や別の自動処理を行わないでください。中止や失敗までの保存結果は残ります。再実行すると重複する場合があります。' />
                    {error && <Alert type='error' showIcon message={error} />}
                    {!loading && !models.length && <Alert type='info' message='利用できる単一画像用モデルがありません。ONNXモデルの登録とサーバー設定を確認してください。' />}
                    <Form layout='vertical' disabled={busy || loading || ran}>
                        <Form.Item label='対象画像の条件'>
                            <Select value={condition.mode} onChange={(mode: Condition['mode']) => {
                                setCondition({ mode, labelIds: mode === 'untagged' ? [] : condition.labelIds }); invalidate();
                            }} options={[
                                { value: 'any', label: '選択タグのいずれかが付いた画像' },
                                { value: 'all', label: '選択タグのすべてが付いた画像' },
                                { value: 'none', label: '選択タグのどれも付いていない画像' },
                                { value: 'untagged', label: 'タグが1個も付いていない画像（図形の有無は不問）' },
                            ]} />
                        </Form.Item>
                        {condition.mode !== 'untagged' && <Form.Item label='判定するタグラベル'>
                            <Select mode='multiple' value={condition.labelIds} optionFilterProp='label'
                                onChange={(labelIds: number[]) => { setCondition({ ...condition, labelIds }); invalidate(); }}
                                options={labels.filter((item) => ['tag', 'any'].includes(item.type))
                                    .map((item) => ({ value: item.id, label: item.name }))} />
                        </Form.Item>}
                        <Form.Item label='推論モデル'>
                            <Select value={modelId} onChange={selectModel} placeholder='モデルを選択'
                                options={models.map((item) => ({ value: String(item.id), label: item.name }))} />
                        </Form.Item>
                        {model?.labels.map((item) => <Form.Item key={item.name} label={`出力先: ${item.name} (${item.type})`}>
                            <Select allowClear placeholder='出力しない' value={choices[item.name] || undefined}
                                onChange={(name?: string) => setChoices({ ...choices, [item.name]: name ?? '' })}
                                options={labels.filter((label) => compatible(item, label))
                                    .map((label) => ({ value: label.name, label: label.name }))} />
                        </Form.Item>)}
                        <Form.Item label='信頼度の閾値（0から1）'>
                            <InputNumber value={threshold} min={0} max={1} step={0.05} onChange={setThreshold} />
                        </Form.Item>
                        <Checkbox checked={convert} onChange={(event) => setConvert(event.target.checked)}>マスクをポリゴンに変換</Checkbox>
                    </Form>
                    <Space wrap>
                        <Button onClick={() => { void preview(); }} disabled={busy || loading ||
                            (condition.mode !== 'untagged' && !condition.labelIds.length)}>対象をプレビュー</Button>
                        <Button type='primary' onClick={() => { void run(); }} disabled={busy || loading || !plan ||
                            !selected.size || !mappingValid || threshold === null || ran}>選択画像に実行</Button>
                        {busy && <Button danger disabled={stopping} onClick={() => {
                            session.cancelled = true; setStopping(true);
                        }}>{stopping ? '現在の通信の終了を待って停止します' : '中止'}</Button>}
                        {ran && <Button disabled={busy} onClick={invalidate}>条件を変更</Button>}
                        <Button disabled={busy} onClick={close}>閉じる</Button>
                        {report && <Button disabled={busy} onClick={downloadReport}>実行結果を保存</Button>}
                    </Space>
                    {plan && <>
                        <div>{`${plan.tasks.length}タスク / 候補${plan.frames.length}画像 / 選択${selected.size}画像`}</div>
                        <Space>
                            <Button disabled={busy || ran} onClick={() => setSelected(new Set(plan.frames.map((frame) => frame.key)))}>全選択</Button>
                            <Button disabled={busy || ran} onClick={() => setSelected(new Set())}>全解除</Button>
                        </Space>
                        <Table<Frame> size='small' rowKey='key' dataSource={plan.frames}
                            pagination={{ pageSize: 20, showSizeChanger: false }}
                            rowSelection={{ selectedRowKeys: [...selected], preserveSelectedRowKeys: true,
                                onChange: (keys) => setSelected(new Set(keys.map(String))),
                                getCheckboxProps: () => ({ disabled: busy || ran }),
                            }} columns={[
                                { title: 'タスク', dataIndex: 'taskId', width: 90 },
                                { title: 'フレーム（0始まり）', dataIndex: 'frame', width: 150 },
                                { title: '画像名', dataIndex: 'name', ellipsis: true },
                                { title: '結果', key: 'result', render: (_: unknown, frame: Frame) => {
                                    const status = statuses.get(frame.key); return status ? statusNames[status] : '';
                                } },
                            ]} />
                    </>}
                    {report && <>
                        <Progress percent={selected.size ? Math.floor(done * 100 / selected.size) : 0}
                            status={report.error ? 'exception' : busy ? 'active' : 'normal'} />
                        <div>{`保存${report.outcomes.filter((item) => item.status === 'saved').length} / 検出なし${report.outcomes.filter((item) => item.status === 'empty').length} / 除外${report.outcomes.filter((item) => item.status === 'skipped').length}`}</div>
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
