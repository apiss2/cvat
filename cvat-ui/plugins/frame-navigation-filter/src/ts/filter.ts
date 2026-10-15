// SPDX-License-Identifier: MIT

// Only Job.annotations.get is intercepted. Search, export and Task APIs retain
// their original filters. Defer the read to core so every hook runs once and
// other enter hooks can still prevent the method before it has any side effects.
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
                                    _plugin: unknown,
                                    frame: number,
                                    allTracks: boolean,
                                    filters: object[],
                                ): Promise<{ methodArguments: unknown[] } | undefined> {
                                    if (!enabled() || !Array.isArray(filters) || !filters.length) {
                                        return undefined;
                                    }
                                    return {
                                        methodArguments: [frame, allTracks, []],
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
