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
        const changeFilters = useCallback((next: object[], valid: boolean): void => {
            const signature = JSON.stringify([next, valid]);
            if (previousFilters.current === signature) return;
            previousFilters.current = signature;
            setFilters(next); setFilterValid(valid); setPreview(undefined); setError('');
        }, []);
        const run = async (exporting: boolean): Promise<void> => {
            if (session.busy || session.disposed || enqueued || !options || !filterValid ||
                (exporting && (!preview?.count || !format))) return;
            session.busy = true; setBusy(true); setError(''); setUncertain(false);
            try {
                if (exporting && preview) {
                    await api.export({ filters, format, saveImages }, preview.revision);
                    if (!session.disposed) setEnqueued(true);
                } else {
                    setPreview(undefined);
                    const result = await api.preview(filters);
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
                onCancel={() => { if (!session.busy) dispose(); }} closable={!busy} maskClosable={false} keyboard={!busy}
                footer={[
                    <Button key='close' disabled={busy} onClick={dispose}>閉じる</Button>,
                    <Button key='preview' disabled={!options || !filterValid || busy || enqueued}
                        onClick={() => void run(false)}>対象件数を確認</Button>,
                    <Button key='export' type='primary' disabled={!preview?.count || !filterValid || !format || busy || enqueued}
                        loading={busy} onClick={() => void run(true)}>エクスポート</Button>,
                ]}
            >
                <Space direction='vertical' style={{ width: '100%' }}>
                    <Alert type='info' message='条件に合うアノテーションがある画像を選択します。'
                        description='表示フィルターと同じく、条件は一つのアノテーションに対して評価します。選択画像の他のアノテーションは除外しません。保存済みデータを使用し、元データは変更しません。' />
                    {error && <Alert type='error' message={error} />}
                    {uncertain && <Alert type='warning' message='応答を確認できなかった場合も、処理が開始されている可能性があります。再実行前にRequestsを確認してください。' />}
                    {(enqueued || uncertain) && <a href='/requests'>Requestsで処理状況とダウンロードを確認</a>}
                    {enqueued && <Alert type='success' message='エクスポートを受け付けました。この画面は閉じられます。' />}
                    {options && <FilterEditor labels={options.labels} disabled={busy || enqueued} onChange={changeFilters} />}
                    <span>例: Typeがtag、かつLabelが対象のタグ名。条件が空の場合はすべての有効画像を選択します。</span>
                    <Form layout='vertical' disabled={!options || busy || enqueued}>
                        <Form.Item label='出力形式'>
                            <Select value={format || undefined} options={options?.formats.map((name) => ({ value: name, label: name }))}
                                onChange={setFormat} />
                        </Form.Item>
                        <Form.Item><Checkbox checked={saveImages} onChange={(event) => setSaveImages(event.target.checked)}>画像を含める</Checkbox></Form.Item>
                    </Form>
                    <span>保存できるアノテーションや未対応データの扱いは、通常の各出力形式の仕様に従います。</span>
                    {preview && <>
                        <Alert type={preview.count ? 'info' : 'warning'} message={`出力対象: ${preview.count}フレーム`} />
                        <Table size='small' rowKey='id' dataSource={preview.tasks} pagination={{ pageSize: 8 }} columns={[
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
