import asyncio
import json
from fastapi import FastAPI,Request
from pydantic import BaseModel
from registry.http_limits import GuardMiddleware


def invoke(app,chunks):
    messages=[{'type':'http.request','body':chunk,'more_body':i<len(chunks)-1} for i,chunk in enumerate(chunks)];out=[]
    async def receive():return messages.pop(0) if messages else {'type':'http.disconnect'}
    async def send(message):out.append(message)
    asyncio.run(app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST','scheme':'http','path':'/','raw_path':b'/','query_string':b'','headers':[(b'content-type',b'application/json')],'client':('127.0.0.1',1),'server':('test',80)},receive,send))
    return next(m['status'] for m in out if m['type']=='http.response.start')

def test_actual_chunked_body_limited_before_json_parsing():
    app=FastAPI();app.add_middleware(GuardMiddleware,max_bytes=8)
    @app.post('/')
    def handle(body:dict):return body
    assert invoke(app,[b'{"x":"',b'1234567890"}'])==413

def test_chunked_valid_body_allowed():
    app=FastAPI();app.add_middleware(GuardMiddleware,max_bytes=8)
    @app.post('/')
    def handle(body:dict):return body
    assert invoke(app,[b'{"x"',b':1}'])==200
