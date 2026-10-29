// SPDX-License-Identifier: MIT
import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { UploadOutlined } from '@ant-design/icons';
import { Alert, Button, Form, Modal, Radio, Select, Space, Typography } from 'antd';
import type { API, ImportMode } from './api';
import { DICOM_FORMAT, errorMessage } from './messages';

export function openDialog(api: API, projectName: string, closed: () => void,
    accepted: (requestID: string) => void): { dispose: () => void; focus: () => void } {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const session = { disposed: false, busy: false, submitted: false };
    const dispose = (): void => {
        if (session.disposed) return;
        session.disposed = true;
        api.dispose(); root.unmount(); host.remove(); closed();
    };
    function Dialog(): JSX.Element {
        const [formats, setFormats] = useState<string[]>([]);
        const [loadingFormats, setLoadingFormats] = useState(true);
        const [formatsError, setFormatsError] = useState('');
        const [format, setFormat] = useState('');
        const [mode, setMode] = useState<ImportMode>('append');
        const [file, setFile] = useState<File>();
        const [busy, setBusy] = useState(false);
        const [submitted, setSubmitted] = useState(false);
        const [error, setError] = useState('');
        const [requestID, setRequestID] = useState('');
        const picker = useRef<HTMLInputElement>(null);
        const locked = busy || submitted;
        useEffect(() => {
            api.formats().then((values) => {
                if (session.disposed) return;
                setFormats(values);
                setFormat(values.includes('CVAT 1.1') ? 'CVAT 1.1' : values[0]);
            }).catch((reason) => {
                if (!session.disposed) setFormatsError(errorMessage(reason));
            }).finally(() => {
                if (!session.disposed) setLoadingFormats(false);
            });
        }, []);
        const submit = async (): Promise<void> => {
            if (session.busy || session.submitted || session.disposed || !file || !format) return;
            session.busy = true; session.submitted = true;
            setBusy(true); setSubmitted(true); setError('');
            try {
                const id = await api.start(file, format, mode);
                if (!session.disposed) setRequestID(id);
                // Monitoring is independent from this dialog and must survive closing it.
                void Promise.resolve().then(() => accepted(id)).catch(() => {
                    if (!session.disposed) setError('取り込みは受け付けましたが、結果を確認できませんでした。Requestsで確認してください。');
                });
            } catch (reason) {
                if (!session.disposed) setError(errorMessage(reason));
            } finally {
                session.busy = false;
                if (!session.disposed) setBusy(false);
            }
        };
        return (
            <Modal open title={`Import annotations: ${projectName}`} width={640}
                onCancel={() => { if (!session.busy) dispose(); }} closable={!busy}
                maskClosable={false} keyboard={!busy}
                footer={<Space>
                    <Button onClick={dispose} disabled={busy}>閉じる</Button>
                    <Button type='primary' onClick={submit} loading={busy}
                        disabled={locked || !file || !format}>取り込む</Button>
                </Space>}>
                <Space direction='vertical' size='middle' style={{ width: '100%' }}>
                    <Form layout='vertical'>
                        <Form.Item label='取り込み形式' required>
                            <Select aria-label='取り込み形式' value={format || undefined}
                                loading={loadingFormats} disabled={locked || !formats.length}
                                options={formats.map((name) => ({ label: name, value: name }))}
                                onChange={setFormat} showSearch optionFilterProp='label' />
                        </Form.Item>
                        <Form.Item label='更新方法' required style={{ marginBottom: 0 }}>
                            <Radio.Group value={mode} disabled={locked} onChange={(event) => setMode(event.target.value)}>
                                <Radio value='append'>append（追加）</Radio>
                                <Radio value='replace'>replace（置換）</Radio>
                            </Radio.Group>
                        </Form.Item>
                    </Form>
                    {mode === 'replace' ? (
                        <Alert type='warning' showIcon message='対象Taskのアノテーション全体を置換します。'
                            description='入力に含まれない画像のアノテーションも削除します。既存のDicomTagとそのAttributeは保持します。' />
                    ) : (
                        <Alert type='info' message='既存のアノテーションを保持して追加します。'
                            description={format === DICOM_FORMAT ?
                                '同じフレームの同じラベルのTagが既にある場合、そのTaskの取り込みは失敗します。' :
                                'Tagの重複はTaskのImport annotationsと同じ扱いになります。再取り込みでは同じ内容が追加される場合があります。'} />
                    )}
                    <Typography.Paragraph style={{ marginBottom: 0 }}>
                        {format === DICOM_FORMAT ?
                            'records配列を持つJSONを1件だけZIPに入れます。既存のDicomTagからStudyInstanceUIDとSOPInstanceUIDを照合し、Tagを登録します。DicomTagは入力のtagsに指定できません。動画では全フレームに適用します。' :
                            'Task名と完全に一致するフォルダをZIPにまとめます。各フォルダには選択した形式の内部構造をそのまま入れてください。共通の親フォルダは最大1階層まで自動で除外します。入力のDicomTagは取り込みません。'}
                    </Typography.Paragraph>
                    <input ref={picker} type='file' accept='.zip' hidden aria-label='ZIPファイルを選択'
                        disabled={locked} onChange={(event) => {
                            const selected = event.target.files?.[0];
                            event.target.value = '';
                            if (!selected || session.busy || session.submitted) return;
                            if (!selected.size || !selected.name.toLowerCase().endsWith('.zip')) {
                                setFile(undefined); setError('空でないZIPファイルを選択してください。');
                                return;
                            }
                            setFile(selected); setError('');
                        }} />
                    <Space wrap>
                        <Button disabled={locked} icon={<UploadOutlined />} onClick={() => picker.current?.click()}>ZIPファイルを選択</Button>
                        <Typography.Text>{file ? file.name : '未選択'}</Typography.Text>
                    </Space>
                    <Typography.Text type='secondary'>
                        Taskごとに検証して更新します。一つのTaskで失敗しても他のTaskの処理は続行します。
                    </Typography.Text>
                    {(formatsError || error) && <Alert type='error' message={formatsError || error} />}
                    {requestID && <Alert type='success' message='取り込みを受け付けました。'
                        description='ダイアログを閉じた後、画面上部のRequestsから進捗を確認できます。' />}
                </Space>
            </Modal>
        );
    }
    root.render(<Dialog />);
    return { dispose, focus: () => host.querySelector<HTMLElement>('input,button')?.focus() };
}
