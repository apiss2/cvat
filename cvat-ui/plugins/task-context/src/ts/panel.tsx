// SPDX-License-Identifier: MIT

import React, {
    useCallback, useEffect, useId, useRef, useState,
} from '@modules/react';
import {
    DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined,
} from '@ant-design/icons';
import Alert from 'antd/lib/alert';
import Button from 'antd/lib/button';
import Empty from 'antd/lib/empty';
import Input from 'antd/lib/input';
import Modal from 'antd/lib/modal';
import Space from 'antd/lib/space';
import Spin from 'antd/lib/spin';
import Tag from 'antd/lib/tag';
import Tooltip from 'antd/lib/tooltip';
import Typography from 'antd/lib/typography';
import {
    type API, type ContextDraft, type ContextField, type TaskContext,
    errorMessage, responseStatus, validateDraft,
} from './api';
import '../scss/styles.scss';

interface EditableField extends ContextField { key: number }

interface Props {
    taskId: number;
    createAPI: () => API;
}

export default function TaskContextPanel({ taskId, createAPI }: Props): JSX.Element {
    const [context, setContext] = useState<TaskContext>();
    const [loading, setLoading] = useState(true);
    const [loadError, setLoadError] = useState('');
    const [editing, setEditing] = useState(false);
    const [fields, setFields] = useState<EditableField[]>([]);
    const [notes, setNotes] = useState('');
    const [saving, setSaving] = useState(false);
    const [saveError, setSaveError] = useState('');
    const [conflict, setConflict] = useState(false);
    const apiRef = useRef<API>();
    const lifetime = useRef(0);
    const busy = useRef(false);
    const nextFieldKey = useRef(0);
    const confirmation = useRef<ReturnType<typeof Modal.confirm>>();
    const notesId = useId();

    const load = useCallback(async (): Promise<boolean> => {
        const api = apiRef.current;
        const currentLifetime = lifetime.current;
        if (!api || busy.current) return false;
        busy.current = true;
        setLoading(true);
        setLoadError('');
        try {
            const result = await api.read();
            if (currentLifetime !== lifetime.current) return false;
            setContext(result);
            setConflict(false);
            return true;
        } catch (reason) {
            if (currentLifetime === lifetime.current) setLoadError(errorMessage(reason));
            return false;
        } finally {
            if (currentLifetime === lifetime.current) {
                busy.current = false;
                setLoading(false);
            }
        }
    }, []);

    useEffect(() => {
        const api = createAPI();
        apiRef.current = api;
        lifetime.current += 1;
        busy.current = false;
        setContext(undefined);
        setEditing(false);
        setFields([]);
        setNotes('');
        setSaving(false);
        setSaveError('');
        setConflict(false);
        load();
        return () => {
            lifetime.current += 1;
            apiRef.current = undefined;
            api.dispose();
            confirmation.current?.destroy();
        };
    }, [createAPI, load]);

    const newField = (field: ContextField = { name: '', value: '' }): EditableField => {
        nextFieldKey.current += 1;
        return { ...field, key: nextFieldKey.current };
    };
    const draft: ContextDraft = { fields: fields.map(({ name, value }) => ({ name, value })), notes };
    const dirty = !!context &&
        JSON.stringify(draft) !== JSON.stringify({ fields: context.fields, notes: context.notes });
    const empty = !!context && !context.fields.length && !context.notes;

    const edit = (): void => {
        if (!context?.can_edit || loading) return;
        setFields(context.fields.map(newField));
        setNotes(context.notes);
        setSaveError('');
        setEditing(true);
    };

    const confirmDiscard = (title: string, action: () => void): void => {
        if (confirmation.current) return;
        confirmation.current = Modal.confirm({
            title,
            okText: '破棄',
            okButtonProps: { danger: true, autoInsertSpace: false },
            cancelText: '戻る',
            cancelButtonProps: { autoInsertSpace: false },
            onOk: action,
            afterClose: () => { confirmation.current = undefined; },
        });
    };

    const closeEditor = (): void => {
        if (busy.current) return;
        const discard = (): void => {
            setEditing(false);
            if (conflict) {
                load();
            }
        };
        if (dirty) confirmDiscard('変更を破棄しますか？', discard);
        else discard();
    };

    const save = async (): Promise<void> => {
        const api = apiRef.current;
        if (!api || !context?.can_edit || busy.current || conflict) return;
        const validationError = validateDraft(draft);
        setSaveError(validationError);
        if (validationError) return;
        const currentLifetime = lifetime.current;
        busy.current = true;
        setSaving(true);
        try {
            const result = await api.save({
                fields: draft.fields.map(({ name, value }) => ({ name: name.trim(), value })),
                notes: draft.notes,
            }, context.version);
            if (currentLifetime !== lifetime.current) return;
            setContext(result);
            setEditing(false);
        } catch (reason) {
            if (currentLifetime !== lifetime.current) return;
            if (responseStatus(reason) === 409) setConflict(true);
            else setSaveError(errorMessage(reason));
        } finally {
            if (currentLifetime === lifetime.current) {
                busy.current = false;
                setSaving(false);
            }
        }
    };

    const reloadAfterConflict = (): void => {
        confirmDiscard('入力を破棄して再読み込みしますか？', () => {
            load().then((loaded) => {
                if (loaded) {
                    setEditing(false);
                }
            });
        });
    };

    return (
        <section className='cvat-task-context' aria-label='タスク情報'>
            <div className='cvat-task-context-heading'>
                <Space size={8}>
                    <Typography.Text strong>タスク情報</Typography.Text>
                    <Tag>{`Task #${taskId}`}</Tag>
                </Space>
                {context?.can_edit && !loading && (
                    <Tooltip title={empty ? '追加' : '編集'}>
                        <Button type='text' size='small' icon={empty ? <PlusOutlined /> : <EditOutlined />}
                            aria-label={empty ? 'タスク情報を追加' : 'タスク情報を編集'} onClick={edit} />
                    </Tooltip>
                )}
            </div>
            {loadError && (
                <Alert type='error' showIcon message={loadError} action={(
                    <Button size='small' icon={<ReloadOutlined />} disabled={loading}
                        onClick={() => load()}>再試行</Button>
                )} />
            )}
            {loading && <div className='cvat-task-context-loading'><Spin /></div>}
            {!loading && context && (
                // The bounded scroll region needs keyboard focus to expose all its read-only text.
                // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
                <div className='cvat-task-context-content' role='region' aria-label='登録情報' tabIndex={0}>
                    {empty && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description='未登録' />}
                    {!!context.fields.length && (
                        <dl className='cvat-task-context-fields'>
                            {context.fields.map((field) => (
                                <div className='cvat-task-context-field' key={field.name}>
                                    <dt>{field.name}</dt>
                                    <dd>{field.value || <Typography.Text type='secondary'>未入力</Typography.Text>}</dd>
                                </div>
                            ))}
                        </dl>
                    )}
                    {!!context.notes && (
                        <div className='cvat-task-context-notes'>
                            <Typography.Text type='secondary'>メモ</Typography.Text>
                            <p>{context.notes}</p>
                        </div>
                    )}
                </div>
            )}
            <Modal open={editing} title={`タスク情報: Task #${taskId}`} width={760}
                className='cvat-task-context-editor' destroyOnClose maskClosable={false}
                closable={!saving && !loading} keyboard={!saving && !loading} onCancel={closeEditor}
                footer={[
                    <Button key='cancel' autoInsertSpace={false}
                        disabled={saving || loading} onClick={closeEditor}>取消</Button>,
                    <Button key='save' type='primary' autoInsertSpace={false} loading={saving}
                        disabled={loading || conflict || !context?.can_edit || !dirty} onClick={save}>保存</Button>,
                ]}
            >
                <Space direction='vertical' size={16} style={{ width: '100%' }}>
                    {saveError && <Alert type='error' showIcon message={saveError} />}
                    {conflict && (
                        <Alert type='warning' showIcon message='更新が競合しました。入力は保存されていません。'
                            action={<Button size='small' icon={<ReloadOutlined />} disabled={loading}
                                onClick={reloadAfterConflict}>再読み込み</Button>} />
                    )}
                    {editing && loadError && <Alert type='error' showIcon message={loadError} />}
                    <div className='cvat-task-context-editor-fields'>
                        <table>
                            <thead><tr><th>項目</th><th>値</th><th aria-label='操作' /></tr></thead>
                            <tbody>
                                {fields.map((field, index) => (
                                    <tr key={field.key}>
                                        <td><Input aria-label={`項目名 ${index + 1}`} placeholder='項目名'
                                            value={field.name} maxLength={128} disabled={saving || loading}
                                            onChange={(event) => setFields(fields.map((current) => (
                                                current.key === field.key ?
                                                    { ...current, name: event.target.value } : current
                                            )))} /></td>
                                        <td><Input.TextArea aria-label={`値 ${index + 1}`} placeholder='値'
                                            value={field.value} maxLength={4096} autoSize={{ minRows: 1, maxRows: 4 }}
                                            disabled={saving || loading}
                                            onChange={(event) => setFields(fields.map((current) => (
                                                current.key === field.key ?
                                                    { ...current, value: event.target.value } : current
                                            )))} /></td>
                                        <td><Tooltip title='削除'>
                                            <Button type='text' danger icon={<DeleteOutlined />}
                                                aria-label={`項目 ${index + 1} を削除`} disabled={saving || loading}
                                                onClick={() => setFields(fields.filter((current) => (
                                                    current.key !== field.key
                                                )))} />
                                        </Tooltip></td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                    <Button type='dashed' icon={<PlusOutlined />} block
                        disabled={saving || loading || fields.length >= 64}
                        onClick={() => setFields([...fields, newField()])}>項目を追加</Button>
                    <div className='cvat-task-context-editor-notes'>
                        {/* Ant Design forwards this id to its textarea. */}
                        {/* eslint-disable-next-line jsx-a11y/label-has-associated-control */}
                        <label htmlFor={notesId}>メモ</label>
                        <Input.TextArea id={notesId} value={notes} maxLength={16384}
                            autoSize={{ minRows: 3, maxRows: 8 }} disabled={saving || loading}
                            onChange={(event) => setNotes(event.target.value)} />
                    </div>
                </Space>
            </Modal>
        </section>
    );
}
