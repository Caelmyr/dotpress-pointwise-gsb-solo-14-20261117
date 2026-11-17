import io
import json
import math
import numpy as np
from PIL import Image,ImageDraw
from flask import Blueprint,request,jsonify,Response,send_file,render_template
from image_lab.validation import Invalid
from .store import Store
from .core import render,encode,statistics,measurement,INKS
from .delivery import export_plates,export_project,import_project
from . import compare as comparison


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

    def pair_args():
        a=request.args.get('a')
        b=request.args.get('b')
        if not a or not b:
            raise Invalid('对照须指定两份固定结果参数 a 与 b')
        return a,b

    @api.get('/projects/<key>/compare')
    def compare_report(key):
        a,b=pair_args()
        return jsonify(comparison.build_comparison(store,key,a,b))

    @api.get('/projects/<key>/compare/grid/<channel>.png')
    def compare_grid(key,channel):
        if channel not in comparison.CHANNELS:
            raise Invalid('通道无效')
        a,b=pair_args()
        side=request.args.get('side')
        if side not in ('a','b'):
            raise Invalid('对照侧须为 a 或 b')
        rec_a,folder_a,rec_b,folder_b=comparison._result_pair(store,key,a,b)
        geom_a=comparison.page_geometry(rec_a)
        geom_b=comparison.page_geometry(rec_b)
        common=comparison.common_domain(geom_a,geom_b)
        grid=comparison.comparison_grid(geom_a,geom_b,common)
        ga,gb=comparison.grid_arrays(geom_a,geom_b,grid,common,folder_a,folder_b,channel)
        pic=comparison.grid_image(channel,ga if side=='a' else gb)
        return send_file(io.BytesIO(encode(pic)),mimetype='image/png',max_age=0)

    @api.get('/projects/<key>/compare/diff/<channel>.png')
    def compare_diff(key,channel):
        if channel not in comparison.CHANNELS:
            raise Invalid('通道无效')
        a,b=pair_args()
        rec_a,folder_a,rec_b,folder_b=comparison._result_pair(store,key,a,b)
        geom_a=comparison.page_geometry(rec_a)
        geom_b=comparison.page_geometry(rec_b)
        common=comparison.common_domain(geom_a,geom_b)
        grid=comparison.comparison_grid(geom_a,geom_b,common)
        ga,gb=comparison.grid_arrays(geom_a,geom_b,grid,common,folder_a,folder_b,channel)
        return send_file(io.BytesIO(encode(comparison.diff_image(ga,gb))),
                         mimetype='image/png',max_age=0)

    @api.get('/projects/<key>/compare/outside/<side>.png')
    def compare_outside(key,side):
        if side not in ('a','b'):
            raise Invalid('对照侧须为 a 或 b')
        a,b=pair_args()
        rec_a,folder_a,rec_b,folder_b=comparison._result_pair(store,key,a,b)
        geom=comparison.page_geometry(rec_a if side=='a' else rec_b)
        common=comparison.common_domain(comparison.page_geometry(rec_a),
                                        comparison.page_geometry(rec_b))
        return send_file(io.BytesIO(encode(comparison.outside_mask(geom,common))),
                         mimetype='image/png',max_age=0)

    @api.get('/projects/<key>/compare/probe')
    def compare_probe(key):
        a,b=pair_args()
        try:
            x=float(request.args.get('x'))
            y=float(request.args.get('y'))
        except (TypeError,ValueError):
            raise Invalid('探针须给出毫米坐标 x 与 y')
        if not math.isfinite(x) or not math.isfinite(y):
            raise Invalid('探针坐标须为有限数字')
        return jsonify(comparison.probe(store,key,a,b,x,y))

    @api.get('/projects/<key>/compare.zip')
    def compare_archive(key):
        a,b=pair_args()
        blob=comparison.export_comparison(store,key,a,b)
        return send_file(io.BytesIO(blob),download_name='dotpress-comparison.zip',
                         as_attachment=True,max_age=0)

    @api.post('/projects/<key>/measure')
    def measure(key):
        d=body()
        p,source=store.capture(key)
        store.revision(p,d['revision'])
        _,arrays=render(source,p['settings'])
        result=measurement(arrays,d['rect'])
        return jsonify(store.remember_measurement(key,d['revision'],result))

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
