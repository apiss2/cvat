// SPDX-License-Identifier: MIT
import React, { useEffect, useState } from '@modules/react';
import { createRoot } from '@modules/react-dom/client';
import Alert from 'antd/lib/alert';
import Button from 'antd/lib/button';
import Checkbox from 'antd/lib/checkbox';
import Form from 'antd/lib/form';
import InputNumber from 'antd/lib/input-number';
import Modal from 'antd/lib/modal';
import Select from 'antd/lib/select';
import Space from 'antd/lib/space';
import Table from 'antd/lib/table';
import { errorMessage, type API, type Mode, type Options, type Preview, type Selection } from './api';

export interface DialogHandle { focus(): void; dispose(): void }
const modes: { value: Mode; label: string }[] = [
    { value: 'any', label: '選択したタグのいずれかがある' },
    { value: 'all', label: '選択したタグがすべてある' },
    { value: 'none', label: '選択したタグが一つもない' },
    { value: 'untagged', label: 'どのタグもない' },
    { value: 'all_frames', label: 'タグで絞り込まない' },
];

export function openDialog(api: API, title: string, onClose: () => void): DialogHandle {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    const session = { busy: false, disposed: false };
    const dispose = (): void => {
        if (session.disposed) return;
        session.disposed = true;
        api.dispose();
        // Authentication changes can be dispatched during a React update.
        queueMicrotask(() => { root.unmount(); container.remove(); onClose(); });
    };

    function Dialog(): JSX.Element {
        const [options, setOptions] = useState<Options>();
        const [selection, setSelection] = useState<Selection>({
            filter: { mode: 'any', tag_ids: [], start: 0, stop: null, step: 1 },
            format: '', saveImages: true, omitTags: false,
        });
        const [preview, setPreview] = useState<Preview>();
        const [busy, setBusy] = useState(false);
        const [error, setError] = useState('');
        const [enqueued, setEnqueued] = useState(false);
        const [uncertain, setUncertain] = useState(false);
        useEffect(() => {
            api.options().then((result) => {
                if (session.disposed) return;
                setOptions(result);
                setSelection((current) => ({ ...current,
                    format: result.formats.includes('CVAT for images 1.1') ? 'CVAT for images 1.1' : result.formats[0] || '',
                }));
            }).catch((reason) => { if (!session.disposed) setError(errorMessage(reason)); });
        }, []);
        const requiresTags = ['any', 'all', 'none'].includes(selection.filter.mode);
        const valid = !!selection.format && (!requiresTags || selection.filter.tag_ids.length > 0) &&
            (selection.filter.stop === null || selection.filter.stop >= selection.filter.start) &&
            (selection.format !== 'ITGformat 1.0' || selection.omitTags);
        const change = (next: Selection): void => {
            setSelection(next); setPreview(undefined); setError(''); setUncertain(false);
        };
        const run = async (exporting: boolean): Promise<void> => {
            if (session.busy || session.disposed || enqueued || !valid || (exporting && !preview?.count)) return;
            session.busy = true; setBusy(true); setError(''); setUncertain(false);
            try {
                if (exporting && preview) {
                    await api.export(selection, preview.revision);
                    if (!session.disposed) setEnqueued(true);
                } else {
                    setPreview(undefined);
                    const result = await api.preview(selection);
                    if (!session.disposed) setPreview(result);
                }
            } catch (reason) {
                if (!session.disposed) {
                    setError(errorMessage(reason)); setPreview(undefined);
                    setUncertain(exporting);
                }
            } finally {
                session.busy = false;
                if (!session.disposed) setBusy(false);
            }
        };
        const filter = selection.filter;
        return (
            <Modal open wrapClassName='cvat-filtered-export-modal' title={`条件を指定してエクスポート: ${title}`} width={760}
                onCancel={() => { if (!session.busy) dispose(); }} closable={!busy}
                maskClosable={false} keyboard={!busy}
                footer={[
                    <Button key='close' disabled={busy} onClick={dispose}>閉じる</Button>,
                    <Button key='preview' disabled={!options || !valid || busy || enqueued}
                        onClick={() => void run(false)}>対象件数を確認</Button>,
                    <Button key='export' type='primary' disabled={!preview?.count || !valid || busy || enqueued}
                        loading={busy} onClick={() => void run(true)}>エクスポート</Button>,
                ]}
            >
                <Space direction='vertical' style={{ width: '100%' }}>
                    <Alert type='info' message='条件に合う画像と、その画像のアノテーションを出力します。元データは変更しません。'
                        description='画面上の表示フィルターとは独立しています。サーバーに保存済みのアノテーションを使用します。' />
                    {error && <Alert type='error' message={error} />}
                    {uncertain && <Alert type='warning' message='応答を確認できなかった場合も、処理が開始されている可能性があります。再実行前にRequestsを確認してください。' />}
                    {(enqueued || uncertain) && <a href='/requests'>Requestsで処理状況とダウンロードを確認</a>}
                    {enqueued && <Alert type='success' message='エクスポートを受け付けました。この画面は閉じられます。' />}
                    <Form layout='vertical' disabled={!options || busy || enqueued}>
                        <Form.Item label='タグ条件'>
                            <Select value={filter.mode} options={modes} onChange={(mode: Mode) => change({
                                ...selection, filter: { ...filter, mode, tag_ids: ['any', 'all', 'none'].includes(mode) ? filter.tag_ids : [] },
                            })} />
                        </Form.Item>
                        {requiresTags && <Form.Item label='条件に使用するタグ'>
                            <Select mode='multiple' value={filter.tag_ids} optionFilterProp='label'
                                placeholder='タグを選択してください'
                                options={options?.labels.map((label) => ({ value: label.id, label: label.name }))}
                                onChange={(tag_ids: number[]) => change({ ...selection, filter: { ...filter, tag_ids } })} />
                        </Form.Item>}
                        <Form.Item label='フレーム範囲と抽出間隔'
                            help='0から始まるタスク内の番号です。終了番号を含み、空欄は最後までです。各タスクで、ここに指定した開始番号を基準に指定間隔で抽出します。'>
                            <Space wrap>
                                <span>開始</span><InputNumber min={0} max={2147483647} precision={0} value={filter.start}
                                    onChange={(value) => change({ ...selection, filter: { ...filter, start: value ?? 0 } })} />
                                <span>終了</span><InputNumber min={0} max={2147483647} precision={0} value={filter.stop}
                                    onChange={(stop) => change({ ...selection, filter: { ...filter, stop } })} />
                                <span>間隔</span><InputNumber min={1} max={2147483647} precision={0} value={filter.step}
                                    onChange={(value) => change({ ...selection, filter: { ...filter, step: value ?? 1 } })} />
                            </Space>
                        </Form.Item>
                        <Form.Item label='出力形式' style={{ marginTop: 32 }}>
                            <Select value={selection.format || undefined}
                                options={options?.formats.map((name) => ({ value: name, label: name }))}
                                onChange={(format) => change({ ...selection, format })} />
                        </Form.Item>
                        <Form.Item>
                            <Space direction='vertical'>
                                <Checkbox checked={selection.saveImages} onChange={(event) => change({ ...selection, saveImages: event.target.checked })}>画像を含める</Checkbox>
                                <Checkbox checked={selection.omitTags} onChange={(event) => change({ ...selection, omitTags: event.target.checked })}>出力からすべてのタグを除外する</Checkbox>
                                <span>タグの除外は出力内容だけに適用します。画像の選択条件や元データには影響しません。</span>
                            </Space>
                        </Form.Item>
                    </Form>
                    {selection.format === 'ITGformat 1.0' && <Alert type='warning'
                        message='ITGformatはタグを保存できません。「出力からすべてのタグを除外する」を選択してください。' />}
                    <span>矩形、Segmentation、Trackなどの保存可否は出力形式に従います。動画のTrackは選択フレーム上の形状として出力します。</span>
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
