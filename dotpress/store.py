"""Independent editable plate jobs and immutable rendered revisions."""
import copy
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from image_lab.validation import Invalid, Conflict
from .core import decode,encode,settings,INKS


def identifier():
    return uuid.uuid4().hex[:24]


class Store:
    def __init__(self,root):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()

    def directory(self,key):
        if not isinstance(key,str) or len(key)!=24 or any(c not in '0123456789abcdef' for c in key):
            raise Invalid('工程编号无效')
        return self.root/key

    def read(self,key):
        path=self.directory(key)/'project.json'
        if not path.exists():
            raise Invalid('工程不存在')
        return json.loads(path.read_text())

    def write(self,p):
        directory=self.directory(p['id'])
        directory.mkdir(exist_ok=True)
        path=directory/'project.json'
        temp=directory/('write-'+uuid.uuid4().hex+'.json')
        temp.write_text(json.dumps(p,ensure_ascii=False,indent=2))
        os.replace(temp,path)

    def listing(self):
        items=[]
        for path in sorted(self.root.glob('*/project.json')):
            p=json.loads(path.read_text())
            settings_value=p.get('settings') or {}
            separation=settings_value.get('separation','cmyk')
            inks=settings_value.get('spot_inks') or []
            items.append({'id':p['id'],'name':p['name'],'revision':p['revision'],
                          'source':p['source'],'versions':len(p['versions']),
                          'current_result':p['current_result'],
                          'separation':separation,
                          'ink_names':[ink.get('name','') for ink in inks] if separation=='spot' else list(INKS)})
        return sorted(items,key=lambda p:p['name'])

    def create(self,name='新印刷作业'):
        with self.lock:
            p={'id':identifier(),'name':str(name)[:100],'revision':1,'created':time.time(),
               'source':None,'settings':settings(),'versions':[],'results':[],
               'current_result':None,'measurements':[]}
            self.write(p)
            return p

    def revision(self,p,expected):
        if not isinstance(expected,int) or expected!=p['revision']:
            raise Conflict('工程已变化，请刷新后重试')

    def update(self,key,expected,data):
        with self.lock:
            p=self.read(key)
            self.revision(p,expected)
            if 'settings' in data:
                p['settings']=settings({**p['settings'],**data['settings']})
            if 'name' in data:
                p['name']=str(data['name'])[:100]
            p['revision']+=1
            p['current_result']=None
            self.write(p)
            return p

    def upload(self,key,expected,raw):
        source=encode(decode(raw))
        sha=hashlib.sha256(source).hexdigest()+'.png'
        with self.lock:
            p=self.read(key)
            self.revision(p,expected)
            (self.directory(key)/sha).write_bytes(source)
            p['source']=sha
            p['revision']+=1
            p['current_result']=None
            self.write(p)
            return p

    def source(self,p):
        if not p['source']:
            raise Invalid('请先上传图像')
        path=self.directory(p['id'])/p['source']
        if not path.exists():
            raise Invalid('源图不存在')
        return decode(path.read_bytes())

    def capture(self,key):
        with self.lock:
            p=self.read(key)
            source=self.source(p)
            return copy.deepcopy(p),source

    def snapshot(self,key,expected,label):
        with self.lock:
            p=self.read(key)
            self.revision(p,expected)
            if len(p['versions'])>=20:
                raise Invalid('每工程最多20个命名版本')
            p['versions'].append({'id':identifier(),'label':str(label)[:80],
                                  'source':p['source'],'settings':copy.deepcopy(p['settings'])})
            p['revision']+=1
            self.write(p)
            return p

    def restore(self,key,expected,vid):
        with self.lock:
            p=self.read(key)
            self.revision(p,expected)
            version=next((v for v in p['versions'] if v['id']==vid),None)
            if version is None:
                raise Invalid('版本不存在')
            p['source']=version['source']
            p['settings']=settings(version['settings'])
            p['revision']+=1
            p['current_result']=None
            self.write(p)
            return p

    def publish(self,captured,views,report):
        with self.lock:
            current=self.read(captured['id'])
            if current['revision']!=captured['revision']:
                raise Conflict('计算时输入发生变化，未发布旧结果')
            rid=identifier()
            folder=self.directory(current['id'])/rid
            folder.mkdir()
            for name,pic in views.items():
                (folder/(name+'.png')).write_bytes(encode(pic))
            record={'id':rid,'revision':captured['revision'],'source':captured['source'],
                    'settings':captured['settings'],'report':report,'created':time.time()}
            (folder/'result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
            current['results'].append(record)
            current['current_result']=rid
            self.write(current)
            return record

    def result(self,key,rid=None):
        p=self.read(key)
        rid=rid or p['current_result']
        record=next((r for r in p['results'] if r['id']==rid),None)
        if record is None:
            raise Invalid('请先生成印版')
        return record,self.directory(key)/rid

    def remember_measurement(self,key,expected,record):
        with self.lock:
            p=self.read(key)
            self.revision(p,expected)
            item={**record,'id':identifier(),'revision':p['revision']}
            p['measurements'].append(item)
            p['measurements']=p['measurements'][-40:]
            self.write(p)
            return item
