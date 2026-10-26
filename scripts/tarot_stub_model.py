"""Synthetic local OpenAI endpoint for tarot transport acceptance; never a quality evaluation."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        data=json.loads(payload['messages'][-1]['content'])
        refs=[s['ref'] for s in data['materials']]
        citations=''.join('['+r+']' for r in refs)
        text='从十分钟的画画练习开始，给自己留一点试错空间。'+('这些近期生活片段可以提供参考'+citations+'。' if refs else '')+'这张牌提供一个新视角，选择仍由你决定。'
        result={'id':'tarot-local-test','object':'chat.completion','model':'tarot-stub','choices':[{'index':0,'message':{'role':'assistant','content':json.dumps({'text':text,'used_refs':refs},ensure_ascii=False)},'finish_reason':'stop'}]}
        body=json.dumps(result,ensure_ascii=False).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)

if __name__=='__main__':
    ThreadingHTTPServer(('127.0.0.1',18089),Handler).serve_forever()
