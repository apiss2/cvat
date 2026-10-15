# Frame navigation filter

This optional client plugin makes annotation filters navigation-only for image/video Job annotations. The filter dialog and keyboard shortcuts are unchanged. Non-matching masks, shapes and tags remain available in the object list and canvas; manually hidden objects remain hidden. New annotations remain visible after refresh.

## Enable

Append `plugins/frame-navigation-filter` to `CVAT_CLIENT_PLUGINS` in the existing root `.env`, using `:` between entries. Keep any existing values. The shared `cvatctl` manager preserves custom client plugin paths and automatically adds the enabled SAM2/model registry plugins.

```dotenv
CVAT_CLIENT_PLUGINS=plugins/frame-navigation-filter
```

Rebuild/restart with the existing deployment procedure (`./cvatctl build`, then `./cvatctl up`). For direct UI development, append the same path to `CLIENT_PLUGINS` instead. Removing the path and rebuilding restores upstream behavior. Merely merging this PR does not enable the plugin in an existing deployment.

## Scope and maintenance

No existing CVAT source, UI, server, database, or extension-management file is modified. The supported core plugin hook is `cvat.classes.Job.prototype.annotations.get.enter`. It re-enters the public method with an empty filter list, retaining frame validation, caching, track interpolation and manually hidden flags. `annotations.search`, Task APIs, dataset exports, list filters, and audio interval APIs are not intercepted. All callers of filtered `Job.annotations.get` see the navigation-only behavior while enabled; this is deliberately not restricted to tag predicates. The filter still controls frame search.

The hook does not edit the caller's filter array, change hidden flags, or call a private `.implementation`. The empty-filter invocation terminates recursion. The UI plugin destructor disables the hook because core has no unregister method.

Public API re-entry runs other plugins' `enter` and `leave` hooks on both the inner and outer calls. The annotation implementation runs once, but a companion `leave` transformation runs twice (first with empty filters, then with the original filters), regardless of registration order. A future companion hook with non-idempotent effects must account for this; this plugin does not promise exactly-once execution of other hooks. Keep the public API boundary rather than bypassing it through private implementations. Empty-filter reads and reads while this plugin is disabled invoke companion hooks only once.

## Verification

After installing the repository's JavaScript dependencies:

```sh
node --test cvat-ui/plugins/frame-navigation-filter/tests/filter.test.cjs
```

These tests load the actual repository core plugin registry and cover hook binding, one unfiltered read, forward/backward search, invalid arguments, disabled behavior, hidden flags and newly created shapes. They also exercise a companion enter/leave hook registered before and after this plugin, preserving both leave transformations and documenting their two invocations.

Browser acceptance: open an image task with a tag and a mask on the same frame. Apply a tag filter, navigate in both directions using switching-by-filter, draw a new mask, save and reload. Both old and new masks must remain visible while navigation still skips non-matching frames. Verify an intentionally hidden object stays hidden. A full CVAT build and browser acceptance are separate from the isolated tests.
