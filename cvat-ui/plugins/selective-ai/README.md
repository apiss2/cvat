# Selective automatic annotation

An opt-in client plugin for the fork's existing ONNX/Nuclio model registry. A new **タグ条件で自動アノテーション** item is added through the existing project/task action-menu extension points. It opens an independent dialog in the current document; no page or browser tab is added. The original Automatic annotation dialog, core API implementations and server remain unchanged. This PR is independent of the navigation-only filter plugin.

## Enable

Append `plugins/selective-ai` to the existing root `.env` setting `CVAT_CLIENT_PLUGINS`, separated by `:`. Preserve all existing entries. The extension manager already preserves custom client plugins. Rebuild the UI and restart using the existing `./cvatctl build` and `./cvatctl up` procedure. For direct UI development, use `CLIENT_PLUGINS` instead. Merging alone does not enable an existing deployment.

```dotenv
CVAT_CLIENT_PLUGINS=plugins/selective-ai
```

When both independent plugins are installed:

```dotenv
CVAT_CLIENT_PLUGINS=plugins/frame-navigation-filter:plugins/selective-ai
```

Removing the plugin path and rebuilding removes this feature. No database migration or backend deployment change is required.

## Operation

1. Save any annotation edits. Open a task or project action menu and select **タグ条件で自動アノテーション**. The scope is the clicked task or all readable tasks in that one project, including all task-list pages. Multi-selection of several tasks/projects is intentionally disabled to avoid ambiguous scope.
2. Choose images with any/all selected tag labels, none of the selected tag labels, or no tags at all. “No tags” does not mean “no shapes”: a frame with a mask and no tags is eligible. The tag selector includes `tag` and `any` label types. Duplicate tags do not change the predicate. Deleted images are excluded.
3. Select a registered detector, segmenter or classifier (CVAT function kind `detector`). The existing CVAT **LabelsMapper** handles label, attribute and skeleton-point selection, including differently named attributes/points. Its tuple output is adapted to the existing lambda mapping format and revalidated against current labels before inference. Unmapped classes remain omitted; empty top-level mappings are rejected rather than activating server auto-mapping. `any` and `skeleton` are incompatible in both directions, matching the standard mapper and server. Interactors, trackers and ReID are not supported. Mask-to-polygon conversion is optional and performed by the existing server converter.
4. For ordinary Nuclio functions, the confidence threshold is passed as before. For ONNX models registered through this fork's registry, the threshold input is hidden and the dialog states **このモデルの検出条件は変更できません。** The ineffective threshold is omitted from the inference request and execution report. The registry's existing reserved `mr-` function-ID namespace identifies these models; display names, label types and ONNX filenames are not used to guess capabilities. This follows `registry/gateway.py` and `registry/schema.py` and does not extend the model contract. Registry filtering remains in `model.py`.
5. Preview the matching frame list. This does not call inference or write annotations. Uncheck individual images, or clear the selection and select only the desired images. Frame numbers are CVAT's zero-based **task-relative** indices, not source-video indices derived from `start_frame` or frame sampling. Cancellation is checked before and after each HTTP read, including every label/task-list page. Cancelling page 1 prevents page 2 from being requested.
6. Run the selection. The plugin checks current task membership/size, saved tags and deleted frames at the start of each task. A formerly selected frame that no longer matches is skipped. Newly matching frames and newly added tasks are never added to an existing plan. Once processing a task starts, its eligibility snapshot remains fixed for that task. Concurrent edits are not a supported way of controlling an ongoing run.
7. Keep the tab open until completion. Stop finishes the current HTTP operation and prevents subsequent requests. A result from an in-flight inference is discarded when stopped before saving; an in-flight save may already have committed. Completed frames remain saved. Download the JSON execution report to identify saved, empty, skipped, failed and unprocessed frames. The report also records the explicit label/attribute mapping.

## Isolation and safety

Only files under this plugin directory are changed. The menu factories use `projectActions.items` / `taskActions.items` and the existing `plugins.ready` entrypoint; they do not modify upstream menu code or copy the original automatic-annotation dialog. `mapper.tsx` imports the existing `components/model-runner-modal/labels-mapper` and its styles, with a thin data adapter. The mapper's input arrays are memoized to avoid resetting selections on progress/threshold renders; its controls are hidden while running without unmounting their state. The standard component itself is unchanged.

The adapter uses CVAT's shared authenticated Axios instance with the API base from `core.config.backendAPI`. The organization is pinned when opening the dialog. Changing user/organization invalidates the dialog and prevents further requests/saves from the old run; in-flight requests cannot be rolled back. No tokens are requested, persisted, or placed in the report. CVAT's normal per-endpoint authorization remains in effect.

The public endpoints used are:

```text
GET   /api/tasks?project_id=...&page=...&page_size=100
GET   /api/labels?task_id=...             (or project_id)
GET   /api/tasks/{id}
GET   /api/tasks/{id}/data/meta
GET   /api/tasks/{id}/annotations
GET   /api/lambda/requests
POST  /api/lambda/functions/{model_id}   (one task-relative frame)
PATCH /api/tasks/{id}/annotations?action=create
```

Only selected eligible frames are sent to inference. Inference responses are checked for the exact requested frame, mapped label IDs, no existing annotation IDs and no tracks before being appended. The existing server converts mask RLE, classifier tags, attributes and skeletons; the plugin does not duplicate that conversion. Existing annotations are neither deleted nor replaced. Empty results issue no write. `cleanup`, PUT and DELETE are not used. Mismatched/incomplete pagination or deleted selected tag labels fail closed rather than widening the selection.

## Execution limits and tradeoffs

This is a **browser-orchestrated, sequential batch**, not a durable server-queued job. Closing/reloading the tab or losing the session stops orchestration; it cannot resume automatically after a browser/server restart. The browser warns on closing while busy, but the browser may still allow it. Normal automatic annotation remains available for ordinary server-queued whole-task work.

Each task checks for an already active normal automatic-annotation request before starting. This is not a distributed lock: another browser, user or normal automatic-annotation job could start after that check. Do not edit or run other annotation processes against these tasks concurrently. One plugin dialog is allowed per browser tab. Existing jobs are never cancelled by this plugin.

Writes are append-only and not idempotent. Rerunning a frame can produce duplicate annotations. A write timeout/network error may mean the server accepted the request but the response was lost. The plugin stops on the first error, explicitly reports the uncertainty, and does not retry. Review the frame before rerunning it. The project is not a single transaction; earlier successful frames remain saved after any failure. Selecting a task/project authorizes writes only when the user presses the run button; simply opening the dialog or previewing does not change annotations.

Only 2D image/video tasks are supported. A project containing an unsupported or uninitialized task fails preview explicitly rather than silently dropping it. Only server-saved tags participate. Reading tags currently requires the task annotation response, which also includes shapes/tracks; these are not retained in the plan. Plan memory is proportional to the number of candidate frames, and inference/save HTTP overhead is per frame. Large unattended jobs would require a separate durable backend extension and are not implemented here.

## Verification

With the repository's JavaScript dependencies installed:

```sh
node --test cvat-ui/plugins/selective-ai/tests/*.test.cjs
npx tsc --noEmit --strict --target ES2020 --lib ES2020,DOM \
  cvat-ui/plugins/selective-ai/src/ts/engine.ts \
  cvat-ui/plugins/selective-ai/src/ts/api.ts
```

The isolated Node tests transpile the TypeScript implementation and exercise the selection/runner/HTTP adapter/menu factories with deterministic fake CVAT responses. They cover all predicate combinations, project pagination beyond 100 tasks, task-relative indexing, untouched nonselected frames, preserved annotations, classifier tags and masks, output validation, cancellation, uncertain saves, identity changes, and exact menu wiring. Additional review regressions cover cancellation during each label/task-list page and task read, both forbidden `any`/`skeleton` directions, explicit renamed attribute/point mappings, stale attributes, and the presence/absence of threshold request fields.

The mapper tests exercise its data conversion and component wiring with stubbed React hooks and a stubbed LabelsMapper. They check stable input arrays and disabled behavior but are not browser/React integration tests and do not run live model inference. The standalone strict type check covers the engine, HTTP adapter and imported threshold helper, not the entire UI dependency graph.

Browser acceptance on a disposable task/project: enable the plugin, verify both action menus, preview each predicate on mixed tag/mask data, deselect a frame, and run a registered model. Inspect that only selected frames receive new output and that previous annotations survive. Repeat with frame sampling and deleted frames, a project with multiple task pages, a classifier, cancellation during inference, a permission-denied task and a simulated save failure. Verify a context switch stops the run and the ordinary Automatic annotation dialog is unchanged. Also verify that cancelling a paginated preview makes no next-page request, registry models have no threshold control, ordinary Nuclio functions retain it, and the standard mapper supports renamed attributes and skeleton points without allowing `any`/`skeleton` pairs.

A full CVAT build, browser acceptance, permission integration and live model inference still require the target deployment. No merge or production data mutation is part of this PR.
