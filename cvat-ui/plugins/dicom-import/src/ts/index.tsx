// SPDX-License-Identifier: MIT
import Axios from '@modules/axios';
import type { ComponentBuilder, PluginEntryPoint } from 'components/plugins-entrypoint';
import { createAPI } from './api';
import { openDialog } from './dialog';

const builder: ComponentBuilder = ({ core, store, dispatch, actionCreators }) => {
    let dialog: ReturnType<typeof openDialog> | undefined;
    const identity = (): string => {
        const state = store.getState();
        return state.auth.user ? JSON.stringify([state.auth.user.id, state.organizations.current?.id ?? null]) : '';
    };
    let previous = identity();
    const component = (({ targetProps }: { targetProps: {
        projectInstance?: { id: number; name: string; dimension?: string };
    } }) => {
        const project = targetProps.projectInstance;
        return {
            key: 'dicom-import', label: 'DICOM import',
            disabled: !project || Boolean(project.dimension && project.dimension !== '2d') ||
                store.getState().projects.selected.length > 1,
            onClick: () => {
                if (!project || !identity()) return;
                if (dialog) { dialog.focus(); return; }
                const api = createAPI((request) => Axios.request(request), core.config.backendAPI,
                    project.id, store.getState().organizations.current?.slug ?? '', identity);
                dialog = openDialog(api, project.name, () => { dialog = undefined; });
            },
        };
    }) as unknown as Parameters<typeof actionCreators.addUIComponent>[1];
    dispatch(actionCreators.addUIComponent('projectActions.items', component, { weight: 15 }));
    return {
        name: 'DICOM project import',
        globalStateDidUpdate: (): void => {
            const next = identity();
            if (next !== previous) { previous = next; dialog?.dispose(); }
        },
        destructor: (): void => {
            dialog?.dispose();
            dispatch(actionCreators.removeUIComponent('projectActions.items', component));
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
