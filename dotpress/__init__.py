import io
import numpy as np
from PIL import Image,ImageDraw
from flask import Blueprint,request,jsonify,send_file,render_template
from image_lab.validation import Invalid
from .store import Store
from .core import render,encode,statistics,measurement,ink_channels
from .delivery import export_plates,export_project,import_project


def install(app,root):
    store=Store(root)
    app.extensions['dotpress']=store
    api=Blueprint('dotpress',__name__,url_prefix='/press')

    def body():
        value=request.get_json(silent=True)
        if not isinstance(value,dict):
            raise Invalid('请求须为JSON对象')
        return value

    def allowed_views(record):
        views={'source','proof','continuous','error'}
        for channel in ink_channels(record['settings']):
            views.add(channel['key'])
            views.add(channel['key']+'-tone')
        return views

    @app.get('/')
    def home():
        return render_template('press.html')

    @api.get('/health')
    def health():
        return jsonify({'ok':True,'product':'DotPress','version':'1.1'})

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
        record,folder=store.result(key,request.args.get('result'))
        if channel not in allowed_views(record):
            raise Invalid('通道无效')
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

    @api.post('/projects/<key>/demo')
    def spot_demo(key):
        """Attach the synthetic poster and switch this job to a spot palette."""
        p=store.read(key)
        from .core import SPOT_PRESET
        inks=[{'name':ink['name'],'color':ink['color']} for ink in SPOT_PRESET]
        p=store.update(key,p['revision'],{'settings':{'separation':'spot','spot_inks':inks,
                                                      'ink_limit':2.8,'width_mm':100,'height_mm':75,
                                                      'dpi':96,'lpi':18}})
        return jsonify(p)

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
