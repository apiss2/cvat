// SPDX-License-Identifier: MIT
import Axios from '@modules/axios';
import type { ComponentBuilder, PluginEntryPoint } from 'components/plugins-entrypoint';
import { createAPI } from './api';
import { openDialog, type DialogHandle } from './dialog';

type Target = { id: number; name: string; dimension: string };
const builder: ComponentBuilder = ({ core, store, dispatch, actionCreators }) => {
    let dialog: DialogHandle | undefined;
    const identity = (): string => {
        const state = store.getState();
        return state.auth.user ? JSON.stringify([state.auth.user.id, state.organizations.current?.id ?? null]) : '';
    };
    let lastIdentity = identity();
    const entries = (['project', 'task'] as const).map((type) => {
        const component = (({ targetProps }: { targetProps: { taskInstance?: Target; projectInstance?: Target } }) => {
            const target = type === 'task' ? targetProps.taskInstance : targetProps.projectInstance;
            return {
                key: 'filtered-export', label: '条件を指定してエクスポート',
                disabled: !target || target.dimension !== '2d' ||
                    store.getState()[type === 'task' ? 'tasks' : 'projects'].selected.length > 1,
                onClick: () => {
                    if (dialog) { dialog.focus(); return; }
                    if (!target || !identity()) return;
                    const api = createAPI((request) => Axios.request(request), core.config.backendAPI,
                        type, target.id, store.getState().organizations.current?.slug ?? '', identity);
                    dialog = openDialog(api, `${type} #${target.id} ${target.name}`, () => { dialog = undefined; });
                },
            };
        }) as unknown as Parameters<typeof actionCreators.addUIComponent>[1];
        const path = `${type}Actions.items`;
        dispatch(actionCreators.addUIComponent(path, component, { weight: 30 }));
        return { path, component };
    });
    return {
        name: 'Filtered dataset export',
        globalStateDidUpdate: (): void => {
            const next = identity();
            if (next !== lastIdentity) { lastIdentity = next; dialog?.dispose(); }
        },
        destructor: (): void => {
            dialog?.dispose();
            entries.forEach(({ path, component }) => dispatch(actionCreators.removeUIComponent(path, component)));
        },
    };
};
let registered = false;
function register(): void {
    const host = (window as unknown as { cvatUI?: { registerComponent: PluginEntryPoint } }).cvatUI;
    if (host && !registered) { registered = true; host.registerComponent(builder); }
}
window.addEventListener('plugins.ready', register, { once: true });
register();
