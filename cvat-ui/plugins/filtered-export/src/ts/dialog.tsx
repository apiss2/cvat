// SPDX-License-Identifier: MIT
import React, { useCallback, useEffect, useRef, useState } from '@modules/react';
import { createRoot } from '@modules/react-dom/client';
import Alert from 'antd/lib/alert';
import Button from 'antd/lib/button';
import Checkbox from 'antd/lib/checkbox';
import Form from 'antd/lib/form';
import Modal from 'antd/lib/modal';
import Select from 'antd/lib/select';
import Space from 'antd/lib/space';
import Table from 'antd/lib/table';
import { errorMessage, type API, type Options, type Preview } from './api';
import FilterEditor from './filter-editor';

export interface DialogHandle { focus(): void; dispose(): void }

export function openDialog(api: API, title: string, onClose: () => void): DialogHandle {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    const session = { busy: false, disposed: false };
    const dispose = (): void => {
        if (session.disposed) return;
        session.disposed = true;
        api.dispose();
        queueMicrotask(() => { root.unmount(); container.remove(); onClose(); });
    };

    function Dialog(): JSX.Element {
        const [options, setOptions] = useState<Options>();
        const [filters, setFilters] = useState<object[]>([]);
        const [taskFilter, setTaskFilter] = useState<object>({});
        const [jobFilter, setJobFilter] = useState<object>({});
        const [filterValid, setFilterValid] = useState(false);
        const [format, setFormat] = useState('');
        const [saveImages, setSaveImages] = useState(true);
        const [preview, setPreview] = useState<Preview>();
        const [busy, setBusy] = useState(false);
        const [error, setError] = useState('');
        const [enqueued, setEnqueued] = useState(false);
        const [uncertain, setUncertain] = useState(false);
        useEffect(() => {
            api.options().then((result) => {
                if (session.disposed) return;
                setOptions(result);
                setFormat(result.formats.includes('CVAT for images 1.1') ? 'CVAT for images 1.1' : result.formats[0] || '');
            }).catch((reason) => { if (!session.disposed) setError(errorMessage(reason)); });
        }, []);
        const previousFilters = useRef('');
        const changeFilters = useCallback((next: object[], valid: boolean, taskRule: object, jobRule: object): void => {
            const signature = JSON.stringify([next, valid, taskRule, jobRule]);
            if (previousFilters.current === signature) return;
            previousFilters.current = signature;
            setFilters(next); setTaskFilter(taskRule); setJobFilter(jobRule);
            setFilterValid(valid); setPreview(undefined); setError('');
        }, []);
        const run = async (exporting: boolean): Promise<void> => {
            if (session.busy || session.disposed || enqueued || !options || !filterValid ||
                (exporting && (!preview?.count || !format))) return;
            session.busy = true; setBusy(true); setError(''); setUncertain(false);
            try {
                if (exporting && preview) {
                    await api.export({ filters, taskFilter, jobFilter, format, saveImages }, preview.revision);
                    if (!session.disposed) setEnqueued(true);
                } else {
                    setPreview(undefined);
                    const result = await api.preview(filters, taskFilter, jobFilter);
                    if (!session.disposed) setPreview(result);
                }
            } catch (reason) {
                if (!session.disposed) {
                    setError(errorMessage(reason)); setPreview(undefined); setUncertain(exporting);
                }
            } finally {
                session.busy = false;
                if (!session.disposed) setBusy(false);
            }
        };
        return (
            <Modal open wrapClassName='cvat-filtered-export-modal' title={`条件を指定してエクスポート: ${title}`} width={900}
                style={{ top: 12, maxWidth: 'calc(100vw - 24px)', paddingBottom: 0 }}
                styles={{
                    content: { display: 'flex', flexDirection: 'column', maxHeight: 'calc(100dvh - 24px)' },
                    header: { flexShrink: 0, overflowWrap: 'anywhere' },
                    body: { flex: '1 1 auto', minHeight: 0, minWidth: 0, overflow: 'auto' },
                    footer: { flexShrink: 0 },
                }}
                onCancel={() => { if (!session.busy) dispose(); }} closable={!busy} maskClosable={false} keyboard={!busy}
                footer={[
                    <Button key='close' disabled={busy} onClick={dispose}>閉じる</Button>,
                    <Button key='preview' disabled={!options || !filterValid || busy || enqueued}
                        loading={busy && !preview} onClick={() => void run(false)}>対象件数を確認</Button>,
                    <Button key='export' type='primary' disabled={!preview?.count || !filterValid || !format || busy || enqueued}
                        loading={busy && !!preview} onClick={() => void run(true)}>エクスポート</Button>,
                ]}
            >
                <Space direction='vertical' size={12} style={{ width: '100%' }}>
                    {error && <Alert type='error' message={error} />}
                    {uncertain && <Alert type='warning' message={<>再実行前に <a href='/requests'>Requests</a> で受付状況を確認してください。</>} />}
                    {enqueued && <Alert type='success' message={<>受付完了。<a href='/requests'>Requests</a> で処理状況を確認できます。</>} />}
                    {options && <FilterEditor labels={options.labels} disabled={busy || enqueued} onChange={changeFilters} />}
                    <Form layout='vertical' disabled={!options || busy || enqueued}>
                        <Form.Item label='出力形式' style={{ marginBottom: 8 }}>
                            <Select value={format || undefined} options={options?.formats.map((name) => ({ value: name, label: name }))}
                                onChange={setFormat} />
                        </Form.Item>
                        <Form.Item style={{ marginBottom: 0 }}><Checkbox checked={saveImages} onChange={(event) => setSaveImages(event.target.checked)}>画像を含める</Checkbox></Form.Item>
                    </Form>
                    {preview && <>
                        {preview.count ? <strong>出力対象: {preview.count}フレーム</strong> :
                            <Alert type='warning' message='対象画像がありません。' />}
                        <Table size='small' rowKey='id' dataSource={preview.tasks} pagination={{ pageSize: 8 }}
                            scroll={{ x: 480 }} columns={[
                            { title: 'タスクID', dataIndex: 'id' }, { title: 'タスク名', dataIndex: 'name' },
                            { title: '対象数', dataIndex: 'count' }, { title: '有効フレーム数', dataIndex: 'total' },
                        ]} />
                    </>}
                </Space>
            </Modal>
        );
    }
    root.render(<Dialog />);
    return {
        focus: () => { document.querySelector<HTMLElement>('.cvat-filtered-export-modal')?.focus(); },
        dispose,
    };
}
