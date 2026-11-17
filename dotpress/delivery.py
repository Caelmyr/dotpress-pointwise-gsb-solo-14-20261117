"""Physical plates and portable editable jobs, without external paths."""
import io
import json
import hashlib
import zipfile
from image_lab.validation import Invalid
from .core import INKS,encode,decode,settings


def export_plates(store,key,rid=None):
    record,folder=store.result(key,rid)
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
        for name in (*INKS,'proof','continuous','source'):
            z.writestr(name+'.png',(folder/(name+'.png')).read_bytes())
        manifest={'format':'dotpress-plates-1','result':record,
                  'plate_convention':'black=ink, white=paper; separate grayscale C M Y K',
                  'proof':'approximate subtractive RGB simulation, not an ICC press contract proof',
                  'coordinates':'physical page top-left, mm; positive x right and y down',
                  'pixel_centers':'(column+.5,row+.5)*25.4/dpi'}
        z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        z.writestr('README.txt','DotPress合成演示。四独立灰度印版与RGB软打样。尺寸/DPI/网线和原输入修订见manifest。\n')
    return stream.getvalue()


def export_project(store,key):
    p=store.read(key)
    closure={p['source']}|{v['source'] for v in p['versions']}
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
        editable={k:v for k,v in p.items() if k not in ('results','current_result')}
        z.writestr('project.json',json.dumps(editable,ensure_ascii=False,indent=2))
        for source in sorted(closure-{None}):
            z.writestr(source,(store.directory(key)/source).read_bytes())
    return stream.getvalue()


def import_project(store,raw):
    if len(raw)>64*1024*1024:
        raise Invalid('工程包最多64MB')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            entries=z.infolist()
            names=[e.filename for e in entries]
            if len(names)>30 or len(names)!=len(set(names)) or sum(e.file_size for e in entries)>96*1024*1024:
                raise Invalid('工程包预算或成员无效')
            if any('/' in n or '\\' in n for n in names):
                raise Invalid('工程包文件名无效')
            p=json.loads(z.read('project.json'))
            ps=settings(p['settings'])
            versions=p.get('versions',[])
            if not isinstance(versions,list) or len(versions)>20:
                raise Invalid('版本列表无效')
            for v in versions:
                settings(v['settings'])
            closure={p.get('source')}|{v.get('source') for v in versions}
            assets={}
            for source in closure-{None}:
                if not isinstance(source,str) or not source.endswith('.png') or len(source)!=68:
                    raise Invalid('源图名称无效')
                assets[source]=encode(decode(z.read(source)))
    except Invalid:
        raise
    except Exception as error:
        raise Invalid('无法读取DotPress工程') from error
    with store.lock:
        q=store.create(p.get('name','导入印刷作业'))
        rekey={}
        for source,data in assets.items():
            new=hashlib.sha256(data).hexdigest()+'.png'
            (store.directory(q['id'])/new).write_bytes(data)
            rekey[source]=new
        q['source']=rekey.get(p.get('source'))
        q['settings']=ps
        q['versions']=[{**v,'source':rekey.get(v.get('source')),'settings':settings(v['settings'])} for v in versions]
        q['measurements']=[]
        q['revision']+=1
        store.write(q)
        return q
