// SPDX-License-Identifier: MIT

interface AnnotationSession {
    annotations: {
        get(frame: number, allTracks: boolean, filters: object[]): Promise<unknown>;
    };
}

// Only the Job display API is intercepted. Search, export and Task APIs retain
// their original filters. Re-entering with [] preserves all other core checks.
export function createNavigationFilterPlugin(enabled: () => boolean) {
    return {
        name: 'Frame navigation filter',
        description: 'Use annotation filters for navigation without hiding other objects',
        cvat: {
            classes: {
                Job: {
                    prototype: {
                        annotations: {
                            get: {
                                async enter(
                                    this: AnnotationSession,
                                    _plugin: unknown,
                                    frame: number,
                                    allTracks: boolean,
                                    filters: object[],
                                ): Promise<{ preventMethodCallWithReturn: unknown } | undefined> {
                                    if (!enabled() || !Array.isArray(filters) || !filters.length) {
                                        return undefined;
                                    }
                                    return {
                                        preventMethodCallWithReturn: await this.annotations.get(frame, allTracks, []),
                                    };
                                },
                            },
                        },
                    },
                },
            },
        },
    };
}
