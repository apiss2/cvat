#!/usr/bin/env python3
"""Read-only verification. Pipe into cvat_server AND cvat_worker_annotation Python."""
import argparse
import json
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','cvat.settings.production')
import django
django.setup()
from django.conf import settings
from cvat.apps.lambda_manager.views import LambdaGateway

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--require-registry-model',action='store_true')
p.add_argument('--require-sam2',action='store_true',help='Require both SAM2 functions when SAM2 is enabled')
a=p.parse_args()
assert settings.NUCLIO['HOST']=='model-gateway',settings.NUCLIO
assert str(settings.NUCLIO['PORT'])=='8070',settings.NUCLIO
assert settings.NUCLIO['INVOKE_METHOD']=='dashboard',settings.NUCLIO
assert int(settings.NUCLIO['DEFAULT_TIMEOUT'])>=210,settings.NUCLIO
functions=list(LambdaGateway().list())
ids={f.id for f in functions}
if a.require_sam2:
    for expected in ('pth-sam2-interactor','pth-sam2-tracker'):
        assert expected in ids,f'Missing existing SAM2 function: {expected}'
registry=[f for f in functions if f.id.startswith('mr-')]
if a.require_registry_model:assert registry,'Register the demonstration package first'
print(json.dumps({'status':'ok','sam2_required':a.require_sam2,'registry_models':len(registry),'functions':[{'id':f.id,'name':f.name,'kind':str(f.kind),'labels':f.labels} for f in functions]},ensure_ascii=False,indent=2))
