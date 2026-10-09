# Updating an existing ONNX model

Open a published model's detail page and select **このモデルを更新**. Existing
metadata is prefilled. Select only the files to replace; unselected Python code,
ONNX weights, helper Python modules and the sample image are retained from the
active revision. Each replacement ONNX filename must match the existing filename
exactly, including case. Replacement code must be named `model.py`.

## One registration route and one update route

`POST /api/upload` only registers new models. Its multipart fields are `manifest`,
`code`, `weights` and `sample`. Unknown fields, including `model_id` and
`expected_revision`, are rejected with HTTP 422, even when empty. Old update
requests cannot silently create a new model. There is no compatibility update
route and no external ZIP upload route.

Every existing-model update uses `POST /api/models/{model_id}/update`. Send the
required `expected_revision` and whichever optional `manifest`, `code`, `weights`
and `sample` fields are changing. The model ID belongs in the URL, not the form.
Replacing all files is also an update through this same endpoint.

An active published revision is required as the update base. Unpublished failed
registrations do not expose an update button; register the corrected files as a
new model. Deleted records cannot be updated.

## Retained values and publication

Metadata-only, code-only, sample-only and individual weight updates are supported.
Updates retain the weight filename list; they cannot add, remove or rename
weights. The manifest is a partial JSON object. Omitted fields are retained,
explicit empty strings clear optional text, and nested `polygon` settings merge
by key. A supplied `weights` array must equal the active revision's filename list.
No-change requests are rejected.

An update creates a separate complete candidate and runs sample-inference
validation. Internal ZIP assembly and storage validation remain implementation
details. Only a successful candidate becomes active. Old revisions are immutable.
Ownership and expected-revision checks prevent another user's update or a
concurrent update/delete from being overwritten. Failed validation leaves the
published revision unchanged.

```sh
python -m pytest --noconftest components/model_registry/tests/test_partial_updates.py -q
python -m pytest --noconftest components/extensions/tests/test_registration.py -q
node --test components/model_registry/tests/classification-ui.test.cjs
```

The route/assembly and JavaScript handler tests use service/authentication or DOM
doubles. They do not establish real CVAT authentication, ONNX worker inference or
browser integration.
