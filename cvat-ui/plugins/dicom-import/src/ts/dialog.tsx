// SPDX-License-Identifier: MIT
import React, { useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
    Alert, Button, Input, Modal, Progress, Space, Typography,
} from 'antd';
import { pack } from './archive';
import type { API } from './api';

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
        const [files, setFiles] = useState<File[]>([]);
        const [prefix, setPrefix] = useState('');
        const [busy, setBusy] = useState(false);
        const [submitted, setSubmitted] = useState(false);
        const [progress, setProgress] = useState(0);
        const [error, setError] = useState('');
        const [requestID, setRequestID] = useState('');
        const folder = useRef<HTMLInputElement>(null);
        const picker = useRef<HTMLInputElement>(null);
        const submit = async (): Promise<void> => {
            if (busy || submitted) return;
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
                        disabled={submitted || !prefix.trim() || !files.length}>登録</Button></Space>}>
                <Space direction='vertical' style={{ width: '100%' }}>
                    <Input aria-label='データソース接頭辞' placeholder='接頭辞（例: GRD）' value={prefix}
                        disabled={busy || submitted} onChange={(event) => setPrefix(event.target.value)} />
                    <Space>
                        <Button disabled={busy || submitted} onClick={() => folder.current?.click()}>フォルダ</Button>
                        <Button disabled={busy || submitted} onClick={() => picker.current?.click()}>
                            ファイル／圧縮ファイル
                        </Button>
                    </Space>
                    <input type='file' hidden multiple ref={(element) => {
                        (folder as React.MutableRefObject<HTMLInputElement | null>).current = element;
                        element?.setAttribute('webkitdirectory', '');
                    }} onChange={(event) => setFiles(Array.from(event.target.files || []))} />
                    <input type='file' hidden multiple ref={picker} accept='.zip,.tar,.tgz,.gz,.dcm,.dicom'
                        onChange={(event) => setFiles(Array.from(event.target.files || []))} />
                    <div role='region' aria-label='ファイルのドロップ領域'
                        style={{ border: '1px dashed', padding: 16 }}
                        onDragOver={(event) => event.preventDefault()} onDrop={(event) => {
                            event.preventDefault();
                            if (!busy && !submitted) setFiles(Array.from(event.dataTransfer.files));
                        }}>ここにファイルをドロップ</div>
                    <Typography.Text>{files.length}ファイル</Typography.Text>
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
