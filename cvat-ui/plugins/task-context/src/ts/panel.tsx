// SPDX-License-Identifier: MIT

import React, { useEffect, useState } from '@modules/react';
import { EditOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import Alert from 'antd/lib/alert';
import Button from 'antd/lib/button';
import Empty from 'antd/lib/empty';
import Space from 'antd/lib/space';
import Spin from 'antd/lib/spin';
import Tag from 'antd/lib/tag';
import Tooltip from 'antd/lib/tooltip';
import Typography from 'antd/lib/typography';
import MDEditor from '@uiw/react-md-editor';
import rehypeSanitize from 'rehype-sanitize';
import { type API, type TaskGuide, errorMessage } from './api';
import '../scss/styles.scss';

interface Props {
    taskId: number;
    createAPI: () => API;
    onEdit?: () => void;
}

export default function TaskContextPanel({ taskId, createAPI, onEdit }: Props): JSX.Element {
    const [guide, setGuide] = useState<TaskGuide | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [attempt, setAttempt] = useState(0);

    useEffect(() => {
        const api = createAPI();
        let active = true;
        setGuide(null);
        setLoading(true);
        setError('');
        api.read().then((result) => {
            if (active) setGuide(result);
        }).catch((reason) => {
            if (active) setError(errorMessage(reason));
        }).finally(() => {
            if (active) setLoading(false);
        });
        return () => {
            active = false;
            api.dispose();
        };
    }, [createAPI, attempt]);

    const empty = !guide?.markdown.trim();
    return (
        <section className='cvat-task-context' aria-label='タスク情報'>
            <div className='cvat-task-context-heading'>
                <Space size={8}>
                    <Typography.Text strong>タスク情報</Typography.Text>
                    <Tag>{`Task #${taskId}`}</Tag>
                </Space>
                {onEdit && !loading && !error && (
                    <Tooltip title={empty ? '追加' : '編集'}>
                        <Button type='text' size='small' icon={empty ? <PlusOutlined /> : <EditOutlined />}
                            aria-label={empty ? 'タスク情報を追加' : 'タスク情報を編集'} onClick={onEdit} />
                    </Tooltip>
                )}
            </div>
            {error && (
                <Alert type='error' showIcon message={error} action={(
                    <Button size='small' icon={<ReloadOutlined />} disabled={loading}
                        onClick={() => setAttempt((value) => value + 1)}>再試行</Button>
                )} />
            )}
            {loading && <div className='cvat-task-context-loading'><Spin /></div>}
            {!loading && !error && (
                // The bounded scroll region needs keyboard focus to expose all its read-only text.
                // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
                <div className='cvat-task-context-content' role='region' aria-label='登録情報' tabIndex={0}>
                    {empty ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description='未登録' /> : (
                        <MDEditor.Markdown source={guide.markdown}
                            wrapperElement={{ 'data-color-mode': 'light' }}
                            rehypePlugins={[[rehypeSanitize]]} />
                    )}
                </div>
            )}
        </section>
    );
}
