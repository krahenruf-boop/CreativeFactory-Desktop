"""Official Feishu SDK WebSocket listener plus bot-only message delivery."""
import asyncio
import hashlib
import importlib
import json
import logging
import sys
import threading
import time
from pathlib import Path

import httpx


class FeishuTransport:
    def __init__(self, credentials, receive, sdk_path):
        self.credentials, self.receive, self.sdk_path = credentials, receive, Path(sdk_path)
        self.thread = None
        self.client = None
        self.loop = None
        self.state = 'DISABLED'
        self.error = ''
        self.token, self.expires = '', 0
        self.token_lock = threading.Lock()
        self.stopped = False

    def access_token(self):
        with self.token_lock:
            if time.time() < self.expires:
                return self.token
            app_id, secret = self.credentials()
            with httpx.Client(timeout=15) as client:
                result = client.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal', json=dict(app_id=app_id, app_secret=secret)).json()
            if result.get('code') != 0 or not result.get('tenant_access_token'):
                raise ValueError('FEISHU_TASK_AUTH_FAILED')
            self.token = result['tenant_access_token']
            self.expires = time.time() + max(30, result.get('expire', 3600) - 60)
            return self.token

    def request(self, method, path, **kwargs):
        with httpx.Client(timeout=15) as client:
            response = client.request(method, 'https://open.feishu.cn/open-apis' + path, headers={'Authorization': 'Bearer ' + self.access_token()}, **kwargs)
            data = response.json()
        if data.get('code') != 0:
            # Errors expose codes, never request URLs/tokens/response bodies.
            if data.get('code') in {99991663, 99991668}:
                self.expires = 0
            raise ValueError('FEISHU_TASK_API_' + str(data.get('code', response.status_code)))
        return data.get('data', data) or {}

    def send(self, chat, card, key):
        return self.request('POST', '/im/v1/messages', params={'receive_id_type': 'chat_id'},
                            json=dict(receive_id=chat, msg_type='interactive', content=json.dumps(card, ensure_ascii=False),
                                      uuid=hashlib.sha256(key.encode()).hexdigest()[:32]))['message_id']

    def update(self, message, card):
        self.request('PATCH', '/im/v1/messages/' + message, json={'content': json.dumps(card, ensure_ascii=False)})

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.listen, name='feishu-websocket', daemon=True)
        self.stopped = False
        self.thread.start()

    def listen(self):
        try:
            self.state, self.error = 'CONNECTING', ''
            if str(self.sdk_path) not in sys.path:
                sys.path.append(str(self.sdk_path))
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            import lark_oapi as lark
            from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse, CallBackToast
            ws = importlib.import_module('lark_oapi.ws.client')
            ws.loop = self.loop
            # SDK log URLs contain WebSocket credentials: do not log them.
            logging.getLogger('Lark').disabled = True

            def message(event):
                raw = json.loads(lark.JSON.marshal(event))
                self.receive(raw, 'message')

            def action(event):
                raw = json.loads(lark.JSON.marshal(event))
                result = self.receive(raw, 'action')
                return P2CardActionTriggerResponse(dict(toast=dict(type='success' if result['accepted'] else 'error', content=result['message'])))

            handler = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(message).register_p2_card_action_trigger(action).build()
            app_id, secret = self.credentials()
            self.client = lark.ws.Client(app_id, secret, event_handler=handler, log_level=lark.LogLevel.ERROR)
            self.client.start()
        except Exception:
            self.state, self.error = ('DISABLED', '') if self.stopped else ('ERROR', 'FEISHU_TASK_CONNECTION_FAILED')

    def connected(self):
        connected = bool(not self.stopped and self.client and self.client._conn is not None)
        if connected:
            self.state, self.error = 'CONNECTED', ''
        elif not self.stopped and self.state == 'CONNECTED':
            self.state = 'CONNECTING'
        return connected

    def stop(self):
        self.stopped = True
        self.state = 'DISABLED'
        if self.loop and self.loop.is_running():
            async def close():
                if self.client:
                    await self.client._disconnect()
                self.loop.stop()
            asyncio.run_coroutine_threadsafe(close(), self.loop)
