// SPDX-License-Identifier: MIT
import React, { useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { FolderOpenOutlined, InboxOutlined, UploadOutlined } from '@ant-design/icons';
import {
    Alert, Button, Form, Input, Modal, Progress, Space, Spin, theme, Typography,
} from 'antd';
import { pack } from './archive';
import type { API } from './api';
import { collectDroppedFiles, collectFiles } from './selection';

export function openDialog(api: API, projectName: string, closed: () => void): {
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
        const [progress, setProgress] = useState(0);
        const [error, setError] = useState('');
        const [requestID, setRequestID] = useState('');
        const folder = useRef<HTMLInputElement>(null);
        const picker = useRef<HTMLInputElement>(null);
        const locked = collecting || busy || submitted;
        const select = async (read: () => Promise<File[]>): Promise<void> => {
            if (locked) return;
            setCollecting(true); setFiles([]); setError(''); setProgress(0);
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
            if (locked || !prefix.trim() || !files.length) return;
            setBusy(true); setError('');
            try {
                await api.available();
                const archive = await pack(files, prefix, api.signal, (value) => {
                    if (!disposed) setProgress(Math.round(value * 100));
                });
                if (disposed) return;
                setSubmitted(true);
                const id = await api.start(archive);
                if (!disposed) setRequestID(id);
            } catch (reason) {
                if (!disposed) setError(reason instanceof Error ? reason.message : String(reason));
            } finally {
                if (!disposed) setBusy(false);
            }
        };
        return (
            <Modal open title={`DICOM import: ${projectName}`} onCancel={dispose} maskClosable={false}
                footer={<Space><Button onClick={dispose}>閉じる</Button>
                    <Button type='primary' onClick={submit} loading={busy}
                        disabled={locked || !prefix.trim() || !files.length}>登録</Button></Space>}>
                <input type='file' hidden multiple disabled={locked} aria-label='フォルダを選択' ref={(element) => {
                    (folder as React.MutableRefObject<HTMLInputElement | null>).current = element;
                    element?.setAttribute('webkitdirectory', '');
                }} onChange={(event) => {
                    const selected = Array.from(event.target.files || []);
                    event.target.value = '';
                    if (selected.length) void select(() => collectFiles(selected, api.signal));
                }} />
                <input type='file' hidden multiple disabled={locked} ref={picker} aria-label='ファイルを選択'
                    accept='.zip,.tar,.tar.gz,.tgz,.dcm,.dicom'
                    onChange={(event) => {
                        const selected = Array.from(event.target.files || []);
                        event.target.value = '';
                        if (selected.length) void select(() => collectFiles(selected, api.signal));
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
                            event.dataTransfer.dropEffect = locked ? 'none' : 'copy';
                            if (!locked) setDragging(true);
                        }} onDragLeave={() => setDragging(false)} onDrop={(event) => {
                            event.preventDefault();
                            setDragging(false);
                            void select(() => collectDroppedFiles(event.dataTransfer, api.signal));
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
                                {collecting ? 'ファイルを確認中…' : `${files.length.toLocaleString()} ファイル選択済み`}
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
                    {busy && <Progress percent={progress} status='active' />}
                    {error && <Alert type='error' message={error} />}
                    {requestID && <Alert type='success' message='登録を受け付けました。'
                        description={<a href='/requests'>Requestsで進捗を確認</a>} />}
                </Space>
            </Modal>
        );
    }
    root.render(<Dialog />);
    return { dispose, focus: () => host.querySelector<HTMLElement>('input,button')?.focus() };
}
