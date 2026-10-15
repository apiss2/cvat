# Frame navigation filter

This optional client plugin makes annotation filters navigation-only for image/video Job annotations. The filter dialog and keyboard shortcuts are unchanged. Non-matching masks, shapes and tags remain available in the object list and canvas; manually hidden objects remain hidden. New annotations remain visible after refresh.

## Enable

Append `plugins/frame-navigation-filter` to `CVAT_CLIENT_PLUGINS` in the existing root `.env`, using `:` between entries. Keep any existing values. The shared `cvatctl` manager preserves custom client plugin paths and automatically adds the enabled SAM2/model registry plugins.

```dotenv
CVAT_CLIENT_PLUGINS=plugins/frame-navigation-filter
```

Rebuild/restart with the existing deployment procedure (`./cvatctl build`, then `./cvatctl up`). For direct UI development, append the same path to `CLIENT_PLUGINS` instead. Removing the path and rebuilding restores upstream behavior. Merely merging this PR does not enable the plugin in an existing deployment.

Build this plugin together with the `cvat-core/src/plugins.ts` change in this PR. Copying only the plugin onto an unmodified upstream core is not supported: that core does not recognize the new `methodArguments` option.

## Scope and maintenance

The only modified upstream file is `cvat-core/src/plugins.ts`. Its enter-hook options gain an optional `methodArguments` array, which replaces the arguments passed to the implementation without calling the public API again. Existing UI controls, server, database, and extension-management files are unchanged. Hooks not supplying this option retain their existing behavior.

The plugin uses `cvat.classes.Job.prototype.annotations.get.enter` to supply `[frame, allTracks, []]`. Core calls the existing annotation implementation after all enter hooks have completed, retaining frame validation, caching, track interpolation and manually hidden flags. `annotations.search`, Task APIs, dataset exports, list filters, and audio interval APIs are not intercepted. All callers of filtered `Job.annotations.get` see the navigation-only behavior while enabled; this is deliberately not restricted to tag predicates. The filter still controls frame search.

The hook does not edit the caller's filter array, change hidden flags, re-enter `annotations.get`, or call a private `.implementation`. The UI plugin destructor disables the hook because core has no unregister method.

## Single invocation of hooks

A successful public API call runs every registered enter/leave hook once and the annotation implementation once. All hooks see the original caller arguments; only the implementation sees the replacement arguments. Companion leave transformations therefore apply once, regardless of whether the companion is registered before or after this plugin.

Core collects argument replacements before running the implementation. If another enter hook prevents the method or supplies a replacement return value, no annotation read occurs. The existing precedence of `preventMethodCallWithReturn` over `preventMethodCall` remains unchanged. An exception in an enter hook or the implementation still rejects the call without executing leave hooks; leave exceptions propagate without retrying the method.

The last supplied `methodArguments` array in registration order wins. Core validates that it is an array and takes a shallow copy, so later changes to that top-level array do not alter the captured argument list. An empty array supplies zero arguments. Omitting the option preserves the original arguments; explicitly supplying a non-array (including `undefined`) is an error. Replacement arguments are local to each invocation, with no shared recursion flag or temporary suppression of other plugins.

This replaces the earlier re-entrant implementation and its documented double hook execution. Hook duplication is no longer treated as acceptable behavior.

## Verification

After installing the repository's JavaScript dependencies:

```sh
node --test cvat-ui/plugins/frame-navigation-filter/tests/filter.test.cjs
npx tsc --noEmit --strict --target ES2020 --lib ES2020,DOM \
  cvat-ui/plugins/frame-navigation-filter/src/ts/filter.ts
```

The tests load the actual repository core plugin registry. They cover hook binding, one unfiltered read, unchanged search filters, disabled/absent plugin behavior, hidden flags and newly created shapes. Companion hooks are exercised in both registration orders to verify one enter/leave execution, one return-value transformation, prevention of the method, replacement returns, and error propagation. Overlapping calls verify that their arguments and hooks remain independent. Separate cases exercise empty, invalid, and competing argument replacements.

The annotation implementation in these isolated tests is a fixture and the exception import is stubbed; the actual annotation collection, browser rendering, and full CVAT build are not exercised. The strict TypeScript command checks the plugin helper, not the entire CVAT application.

Browser acceptance: open an image task with a tag and a mask on the same frame. Apply a tag filter, navigate in both directions using switching-by-filter, draw a new mask, save and reload. Both old and new masks must remain visible while navigation still skips non-matching frames. Verify an intentionally hidden object stays hidden. A full CVAT build and browser acceptance are separate from the isolated tests.
