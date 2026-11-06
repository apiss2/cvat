// SPDX-License-Identifier: MIT
import React, {
    useEffect, useMemo, useRef, useState,
} from 'react';
import { createRoot } from 'react-dom/client';
import { FolderOpenOutlined, InboxOutlined, UploadOutlined } from '@ant-design/icons';
import {
    Alert, Button, Form, Input, Modal, Progress, Space, Spin, theme, Typography,
} from 'antd';
import type { API, UploadProgress } from './api';
import { collectDroppedFiles, collectFiles } from './selection';

function formatBytes(bytes: number): string {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    const index = bytes ? Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1) : 0;
    return `${(bytes / 1024 ** index).toLocaleString(undefined, { maximumFractionDigits: 1 })} ${units[index]}`;
}

function progressMessage(stage: UploadProgress['stage'] | undefined, failed: boolean): string {
    if (failed) return stage === 'accepting' ? 'サーバーの受付を確認できませんでした。' : '転送が中断しました。';
    if (stage === 'accepting') return '転送完了。サーバーの受付を確認中です。';
    if (stage === 'preparing') return '転送を準備しています。';
    return 'ファイルをアップロードしています。';
}

export function openDialog(api: API, projectName: string, closed: () => void,
    accepted: (requestID: string) => void): {
    dispose: () => void; focus: () => void;
} {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    let disposed = false;
    const dispose = (): void => {
        if (disposed) return;
        disposed = true;
        api.dispose(); root.unmount(); host.remove(); closed();
    };
    function Dialog(): JSX.Element {
        const { token } = theme.useToken();
        const [files, setFiles] = useState<File[]>([]);
        const [prefix, setPrefix] = useState('');
        const [collecting, setCollecting] = useState(false);
        const [dragging, setDragging] = useState(false);
        const [busy, setBusy] = useState(false);
        const [submitted, setSubmitted] = useState(false);
        const [progress, setProgress] = useState<UploadProgress | null>(null);
        const [error, setError] = useState('');
        const [requestID, setRequestID] = useState('');
        const folder = useRef<HTMLInputElement>(null);
        const picker = useRef<HTMLInputElement>(null);
        const totalBytes = useMemo(() => files.reduce((total, file) => total + file.size, 0), [files]);
        const locked = collecting || busy || submitted;
        useEffect(() => {
            if (!submitted || requestID) return undefined;
            const beforeUnload = (event: BeforeUnloadEvent): void => {
                event.preventDefault();
                // eslint-disable-next-line no-param-reassign -- Required by the legacy beforeunload API.
                event.returnValue = '';
            };
            window.addEventListener('beforeunload', beforeUnload);
            return () => window.removeEventListener('beforeunload', beforeUnload);
        }, [submitted, requestID]);
        const select = async (read: () => Promise<File[]>): Promise<void> => {
            if (locked) return;
            setCollecting(true); setFiles([]); setError(''); setProgress(null);
            try {
                const selected = await read();
                if (disposed) return;
                setFiles(selected);
                if (!selected.length) setError('DICOMまたは対応する圧縮ファイルが見つかりませんでした。');
            } catch (reason) {
                if (!disposed) setError(reason instanceof Error ? reason.message : String(reason));
            } finally {
                if (!disposed) setCollecting(false);
            }
        };
        const submit = async (): Promise<void> => {
            if (collecting || busy || requestID || !prefix.trim() || !files.length) return;
            setBusy(true); setError('');
            try {
                await api.available();
                const id = await api.start(files, prefix, (value) => {
                    if (!disposed) { setSubmitted(true); setProgress(value); }
                });
                if (!disposed) setRequestID(id);
                Promise.resolve().then(() => accepted(id)).catch(() => {
                    if (!disposed) setError('登録は受け付けましたが、結果を確認できませんでした。Requestsで確認してください。');
                });
            } catch (reason) {
                if (!disposed) setError(reason instanceof Error ? reason.message : String(reason));
            } finally {
                if (!disposed) setBusy(false);
            }
        };
        const accepting = progress?.stage === 'accepting';
        const fraction = (progress?.confirmedBytes || 0) / Math.max(progress?.totalBytes || 0, 1);
        const percent = requestID ? 100 : Math.min(99.9, Math.floor(fraction * 1000) / 10);
        const stopLabel = accepting ? '受付確認を中止して閉じる' : '転送を中止して閉じる';
        const retryLabel = accepting ? '受付を再確認' : '転送を再開';
        return (
            <Modal open title={`DICOM import: ${projectName}`} onCancel={dispose} maskClosable={false}
                closable={!busy} keyboard={!busy}
                footer={<Space><Button onClick={dispose} danger={busy}>
                    {busy ? stopLabel : '閉じる'}
                </Button>
                <Button type='primary' onClick={submit} loading={busy}
                    disabled={collecting || busy || Boolean(requestID) || !prefix.trim() || !files.length}>
                    {submitted ? retryLabel : '登録'}
                </Button></Space>}>
                <input type='file' hidden multiple disabled={locked} aria-label='フォルダを選択' ref={(element) => {
                    (folder as React.MutableRefObject<HTMLInputElement | null>).current = element;
                    element?.setAttribute('webkitdirectory', '');
                }} onChange={(event) => {
                    const { target } = event;
                    const selected = Array.from(target.files || []);
                    target.value = '';
                    if (selected.length) select(() => collectFiles(selected, api.signal));
                }} />
                <input type='file' hidden multiple disabled={locked} ref={picker} aria-label='ファイルを選択'
                    accept='.zip,.tar,.tar.gz,.tgz,.dcm,.dicom'
                    onChange={(event) => {
                        const { target } = event;
                        const selected = Array.from(target.files || []);
                        target.value = '';
                        if (selected.length) select(() => collectFiles(selected, api.signal));
                    }} />
                <Space direction='vertical' size='middle' style={{ width: '100%' }}>
                    <div role='region' aria-label='フォルダとファイルのドロップ領域' aria-busy={collecting}
                        style={{
                            border: `1px dashed ${dragging ? token.colorPrimary : token.colorBorder}`,
                            borderRadius: token.borderRadiusLG,
                            background: dragging ? token.colorPrimaryBg : token.colorFillAlter,
                            padding: 24,
                            textAlign: 'center',
                        }}
                        onDragOver={(event) => {
                            event.preventDefault();
                            const { dataTransfer } = event;
                            dataTransfer.dropEffect = locked ? 'none' : 'copy';
                            if (!locked) setDragging(true);
                        }} onDragLeave={() => setDragging(false)} onDrop={(event) => {
                            event.preventDefault();
                            setDragging(false);
                            select(() => collectDroppedFiles(event.dataTransfer, api.signal));
                        }}>
                        <Space direction='vertical' size='middle' style={{ width: '100%' }}>
                            <InboxOutlined aria-hidden style={{ fontSize: 32, color: token.colorPrimary }} />
                            <Typography.Text>フォルダまたはファイルをドロップ</Typography.Text>
                            <Space wrap style={{ justifyContent: 'center' }}>
                                <Button icon={<FolderOpenOutlined aria-hidden />} disabled={locked}
                                    onClick={() => folder.current?.click()}>フォルダを選択</Button>
                                <Button icon={<UploadOutlined aria-hidden />} disabled={locked}
                                    onClick={() => picker.current?.click()}>ファイルを選択</Button>
                            </Space>
                            <Typography.Text type='secondary'>DICOM / ZIP / TAR / TAR.GZ / TGZ</Typography.Text>
                        </Space>
                    </div>
                    <div role='status' aria-live='polite'>
                        <Space>
                            {collecting && <Spin size='small' />}
                            <Typography.Text type={files.length ? undefined : 'secondary'}>
                                {collecting ? 'ファイルを確認中…' :
                                    `${files.length.toLocaleString()} ファイル選択済み / ${formatBytes(totalBytes)}`}
                            </Typography.Text>
                        </Space>
                    </div>
                    <Form layout='vertical'>
                        <Form.Item label='タスク名の接頭辞' required htmlFor='cvat-dicom-prefix'
                            style={{ marginBottom: 0 }}>
                            <Input id='cvat-dicom-prefix' required aria-required='true' placeholder='例: GRD'
                                value={prefix} disabled={busy || submitted}
                                onChange={(event) => setPrefix(event.target.value)} />
                        </Form.Item>
                    </Form>
                    {progress && !requestID && <div role='status' aria-live='polite'>
                        <Space direction='vertical' style={{ width: '100%' }}>
                            <Space>
                                {busy && <Spin size='small' />}
                                <Typography.Text>
                                    {progressMessage(progress.stage, Boolean(error))}
                                </Typography.Text>
                            </Space>
                            {!accepting && <Progress percent={percent} status={error ? 'exception' : 'active'} />}
                            <Typography.Text>
                                サーバー受信済み: {formatBytes(progress.confirmedBytes)} / {formatBytes(progress.totalBytes)}
                                {' / '}{progress.completedFiles.toLocaleString()} / {progress.totalFiles.toLocaleString()} ファイル
                            </Typography.Text>
                            <Typography.Text type='secondary'>
                                受付が完了するまで、この画面を開いたままにしてください。
                                閉じると転送や受付確認が中断し、この画面からの再開はできなくなります。
                            </Typography.Text>
                        </Space>
                    </div>}
                    {error && <Alert type='error' message={error} />}
                    {requestID && <Alert type='success' message='登録を受け付けました。'
                        description='変換とタスク作成はサーバーで続行します。この画面を閉じても処理は続き、画面上部のRequestsから進捗を確認できます。' />}
                </Space>
            </Modal>
        );
    }
    root.render(<Dialog />);
    return { dispose, focus: () => host.querySelector<HTMLElement>('input,button')?.focus() };
}
