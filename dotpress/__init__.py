import io
import json
import math
import numpy as np
from PIL import Image,ImageDraw
from flask import Blueprint,request,jsonify,Response,send_file,render_template
from image_lab.validation import Invalid, Conflict
from .store import Store
from .core import render,encode,statistics,measurement,INKS
from .delivery import export_plates,export_project,import_project
from .diagnosis import diagnose,preview,fingerprint


def install(app,root):
    store=Store(root)
    app.extensions['dotpress']=store
    api=Blueprint('dotpress',__name__,url_prefix='/press')

    def body():
        value=request.get_json(silent=True)
        if not isinstance(value,dict):
            raise Invalid('请求须为JSON对象')
        return value

    @app.get('/')
    def home():
        return render_template('press.html')

    @api.get('/health')
    def health():
        return jsonify({'ok':True,'product':'DotPress','version':'1.0'})

    @api.get('/projects')
    def listing():
        return jsonify(store.listing())

    @api.post('/projects')
    def create():
        return jsonify(store.create(body().get('name','新印刷作业'))),201

    @api.get('/projects/<key>')
    def read(key):
        return jsonify(store.read(key))

    @api.patch('/projects/<key>')
    def update(key):
        data=body()
        return jsonify(store.update(key,data['revision'],data))

    @api.post('/projects/<key>/source')
    def source(key):
        file=request.files.get('file')
        if file is None:
            raise Invalid('请选择照片')
        return jsonify(store.upload(key,int(request.form['revision']),file.read()))

    @api.post('/projects/<key>/render')
    def generate(key):
        data=body()
        captured,source=store.capture(key)
        store.revision(captured,data['revision'])
        views,arrays=render(source,captured['settings'])
        report=statistics(arrays,captured['settings'])
        record=store.publish(captured,views,report)
        return jsonify(record)

    @api.get('/projects/<key>/result/<channel>.png')
    def result_image(key,channel):
        if channel not in ('source','proof','continuous',*INKS,*[i+'-tone' for i in INKS]):
            raise Invalid('通道无效')
        record,folder=store.result(key,request.args.get('result'))
        return send_file(folder/(channel+'.png'),mimetype='image/png',max_age=0)

    @api.get('/projects/<key>/report')
    def report(key):
        record,_=store.result(key,request.args.get('result'))
        return jsonify(record)

    @api.post('/projects/<key>/measure')
    def measure(key):
        d=body()
        p,source=store.capture(key)
        store.revision(p,d['revision'])
        _,arrays=render(source,p['settings'])
        result=measurement(arrays,d['rect'])
        return jsonify(store.remember_measurement(key,d['revision'],result))

    @api.post('/projects/<key>/diagnose')
    def diagnose_report(key):
        d=body()
        captured,source=store.capture(key)
        store.revision(captured,d['revision'])
        report=diagnose(source,captured['settings'],d.get('rect'))
        report['project']=key
        report['revision']=captured['revision']
        report['input_fingerprint']=fingerprint(captured['source'],captured['settings'])
        return jsonify(report)

    @api.get('/projects/<key>/diagnose/preview.png')
    def diagnose_preview(key):
        def list_arg(name,aliases=()):
            values=[v for v in request.args.getlist(name,type=float) if v is not None]
            for alias in aliases:
                values.extend(v for v in request.args.getlist(alias,type=float) if v is not None)
            if not values:
                raw=request.args.get(name,'') or request.args.get(next(iter(aliases),''),'')
                try:
                    values=[float(v) for v in raw.split(',') if v!='']
                except ValueError as error:
                    raise Invalid(name+'参数须为数字') from error
            if any(not math.isfinite(v) for v in values):
                raise Invalid(name+'参数须为有限数字')
            return values
        revision=request.args.get('revision',type=int)
        rect=list_arg('rect')
        angles=list_arg('angle',('angles',))
        view=request.args.get('view','proof')
        if len(rect)!=4 or len(angles)!=4:
            raise Invalid('预览需要四项区域和四项网角')
        captured,source=store.capture(key)
        store.revision(captured,revision)
        png=preview(source,captured['settings'],rect,angles,view)
        return send_file(io.BytesIO(png),mimetype='image/png',max_age=0)

    @api.post('/projects/<key>/diagnose/apply')
    def diagnose_apply(key):
        d=body()
        captured,_=store.capture(key)
        store.revision(captured,d['revision'])
        expected_fp=fingerprint(captured['source'],captured['settings'])
        if d.get('input_fingerprint')!=expected_fp:
            raise Conflict('诊断输入已变化（源图或设置改动），请重新诊断后再应用')
        angles=d.get('angles')
        if not isinstance(angles,list) or len(angles)!=4:
            raise Invalid('请选择一组四通道网角')
        advice={'label':d.get('label','网花诊断推荐'),
                'input_fingerprint':expected_fp}
        p=store.apply_angles(key,d['revision'],angles,advice)
        return jsonify(p)

    @api.post('/projects/<key>/versions')
    def version(key):
        d=body()
        return jsonify(store.snapshot(key,d['revision'],d.get('label','印版方案')))

    @api.post('/projects/<key>/versions/<vid>/restore')
    def restore(key,vid):
        return jsonify(store.restore(key,body()['revision'],vid))

    @api.get('/projects/<key>/plates.zip')
    def plates(key):
        return send_file(io.BytesIO(export_plates(store,key,request.args.get('result'))),download_name='dotpress-plates.zip',as_attachment=True)

    @api.get('/projects/<key>/project.zip')
    def archive(key):
        return send_file(io.BytesIO(export_project(store,key)),download_name='dotpress-project.zip',as_attachment=True)

    @api.post('/import')
    def importing():
        file=request.files.get('file')
        if file is None:
            raise Invalid('请选择DotPress工程')
        return jsonify(import_project(store,file.read())),201

    @api.post('/demo')
    def demo():
        p=store.create('合成演示：彩色海报与灰阶')
        n=384
        y,x=np.mgrid[:256,:n].astype(float)
        a=np.zeros((256,n,4),dtype='uint8')
        a[:,:,0]=(x/(n-1)*255).astype('uint8')
        a[:,:,1]=(y/255*255).astype('uint8')
        a[:,:,2]=(127+100*np.sin(x/17)*np.cos(y/23)).astype('uint8')
        a[:,:,3]=255
        im=Image.fromarray(a)
        draw=ImageDraw.Draw(im)
        draw.rectangle((20,20,150,80),fill=(10,20,35,255))
        draw.text((32,42),'DOT PRESS',fill=(255,255,255,255))
        draw.ellipse((240,35,340,135),fill=(200,35,65,180))
        p=store.upload(p['id'],p['revision'],encode(im))
        p=store.update(p['id'],p['revision'],{'settings':{'width_mm':100,'height_mm':75,'dpi':96,'lpi':18}})
        return jsonify(p),201

    app.register_blueprint(api)
