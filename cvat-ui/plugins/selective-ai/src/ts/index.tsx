// SPDX-License-Identifier: MIT
import Axios from '@modules/axios';
import type { ComponentBuilder, PluginEntryPoint } from 'components/plugins-entrypoint';
import { createAPI } from './api';
import { openDialog, type DialogHandle } from './dialog';
import type { Scope, Model } from './engine';

type Target = { id: number; name: string; dimension: string };
const builder: ComponentBuilder = ({ core, store, dispatch, actionCreators }) => {
    let dialog: DialogHandle | undefined;
    const identity = (): string => {
        const state = store.getState();
        return state.auth.user ? JSON.stringify([state.auth.user.id, state.organizations.current?.id ?? null]) : '';
    };
    let lastIdentity = identity();
    const open = (scope: Scope, target: Target): void => {
        if (dialog) { dialog.focus(); return; }
        if (!identity()) return;
        const api = createAPI((request) => Axios.request(request), core.config.backendAPI,
            store.getState().organizations.current?.slug ?? '', identity);
        dialog = openDialog({
            api, scope, title: `${scope.type} #${target.id} ${target.name}`,
            loadModels: async () => (await core.lambda.list()).models as unknown as Model[],
            onClose: () => { dialog = undefined; },
        });
    };
    const entries = (['project', 'task'] as const).map((type) => {
        // Both upstream menus call a factory with targetProps and expect a menu
        // item object, not a React element. Do not modify either original menu.
        const component = (({ targetProps }: { targetProps: { taskInstance?: Target; projectInstance?: Target } }) => {
            const target = type === 'task' ? targetProps.taskInstance : targetProps.projectInstance;
            return {
                key: 'selective-ai', label: 'タグ条件で自動アノテーション',
                disabled: !target || target.dimension !== '2d' || store.getState()[type === 'task' ? 'tasks' : 'projects'].selected.length > 1,
                onClick: () => { if (target) open({ type, id: target.id }, target); },
            };
        }) as unknown as Parameters<typeof actionCreators.addUIComponent>[1];
        const path = `${type}Actions.items`;
        dispatch(actionCreators.addUIComponent(path, component, { weight: 25 }));
        return { path, component };
    });
    return {
        name: 'Selective automatic annotation',
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
