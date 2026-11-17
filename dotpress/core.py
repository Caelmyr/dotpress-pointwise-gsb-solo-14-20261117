"""Raster plate preparation in physical page coordinates."""
import io
import math
import hashlib
import numpy as np
from PIL import Image, ImageOps
from scipy.ndimage import map_coordinates
from image_lab.validation import Invalid

INKS=('C','M','Y','K')
# Fixed fallback transmittances (reflectance fraction per sRGB channel).
# Used for unmeasured inks and whenever calibration is disabled.
BASE_ABSORB=np.array([[.04,.83,.88],[.84,.06,.80],[.80,.84,.03],[.08,.08,.08]])
PAIRS=(('C','M'),('C','Y'),('C','K'),('M','Y'),('M','K'),('Y','K'))
REFLECT_EPS=1e-4
RESIDUAL_LIMIT=3.
DEFAULTS={
    'width_mm':100.,'height_mm':75.,'dpi':96.,'lpi':18.,
    'black':.7,'ink_limit':2.8,'gamma':1.,'saturation':1.,
    'paper':'#fff8e8','gain':.12,'proof_density':1.,
    'angles':[15.,75.,0.,45.],'phase_x':[0.,0.,0.,0.],
    'phase_y':[0.,0.,0.,0.],'offset_x':[0.,0.,0.,0.],
    'offset_y':[0.,0.,0.,0.],'enabled':[True,True,True,True],
    'crop':[0.,0.,1.,1.],'fit':'contain','screened':True,
}

def finite(value,label,low,high):
    try:
        number=float(value)
    except (ValueError,TypeError):
        raise Invalid(label+'必须为数字')
    if not math.isfinite(number) or not low<=number<=high:
        raise Invalid(label+f'须在{low}至{high}之间')
    return number


def settings(raw=None):
    raw=raw or {}
    if not isinstance(raw,dict):
        raise Invalid('印版参数须为对象')
    p={**DEFAULTS,**raw}
    ranges={'width_mm':(10,300),'height_mm':(10,300),'dpi':(36,300),
            'lpi':(3,80),'black':(0,1),'ink_limit':(1,4),'gamma':(.2,4),
            'saturation':(0,2),'gain':(-.3,.5),'proof_density':(.2,2.5)}
    for key,(lo,hi) in ranges.items():
        p[key]=finite(p[key],key,lo,hi)
    for key in ('angles','phase_x','phase_y','offset_x','offset_y'):
        if not isinstance(p[key],list) or len(p[key])!=4:
            raise Invalid(key+'须为四通道数组')
        lo,hi=(-180,180) if key=='angles' else (-20,20)
        p[key]=[finite(v,key,lo,hi) for v in p[key]]
    if not isinstance(p['enabled'],list) or len(p['enabled'])!=4:
        raise Invalid('启用通道须为四项')
    p['enabled']=[bool(v) for v in p['enabled']]
    if not isinstance(p['crop'],list) or len(p['crop'])!=4:
        raise Invalid('裁切区域须为四项')
    p['crop']=[finite(v,'裁切坐标',0,1) for v in p['crop']]
    l,t,r,b=p['crop']
    if r-l<.01 or b-t<.01:
        raise Invalid('裁切区域为空')
    if p['fit'] not in ('contain','cover','stretch'):
        raise Invalid('适配方式无效')
    p['screened']=bool(p['screened'])
    p['calibration']=validate_calibration(p.get('calibration'))
    w,h=pixel_size(p)
    if max(w,h)>1200 or w*h>1200**2:
        raise Invalid('当前页面像素预算为1200×1200，请降低DPI或尺寸')
    if p['lpi']>p['dpi']/2:
        raise Invalid('网线频率不能超过DPI的一半')
    return p


def pixel_size(p):
    return (max(1,round(p['width_mm']/25.4*p['dpi'])),
            max(1,round(p['height_mm']/25.4*p['dpi'])))


def hex_rgb(text,label='颜色'):
    if not isinstance(text,str) or len(text)!=7 or text[0]!='#':
        raise Invalid(label+'须为#RRGGBB')
    try:
        return tuple(int(text[i:i+2],16) for i in (1,3,5))
    except ValueError as error:
        raise Invalid(label+'无效') from error


def parse_color(text):
    return np.array(hex_rgb(text,'纸色'),dtype=float)/255


def default_calibration(paper=None):
    """Seed values from the fixed transmittance model on the current paper."""
    paper_linear=linear(parse_color(paper or DEFAULTS['paper']))
    paper_hex='#%02x%02x%02x'%tuple(int(round(v*255)) for v in srgb(paper_linear))
    patches={}
    for i,ink in enumerate(INKS):
        solid=np.clip(paper_linear*BASE_ABSORB[i],0,1)
        patches[ink]={'rgb':'#%02x%02x%02x'%tuple(int(round(v*255)) for v in srgb(solid)),
                      'measured':False}
    for a,b in PAIRS:
        trans=BASE_ABSORB[INKS.index(a)]*BASE_ABSORB[INKS.index(b)]
        solid=np.clip(paper_linear*trans,0,1)
        patches[a+b]={'rgb':'#%02x%02x%02x'%tuple(int(round(v*255)) for v in srgb(solid)),
                      'measured':False}
    return {'enabled':False,'paper':{'rgb':paper_hex,'measured':False},'patches':patches,
            'note':'未实测色块由固定透射率与独立叠印乘积近似；不是ICC合同打样。'}


def validate_calibration(raw):
    if raw is None:
        return default_calibration()
    if not isinstance(raw,dict):
        raise Invalid('软打样校准须为对象')
    cal=default_calibration(raw.get('paper',{}).get('rgb')
                           if isinstance(raw.get('paper'),dict) else None)
    cal['enabled']=bool(raw.get('enabled',False))
    paper=raw.get('paper')
    if isinstance(paper,dict):
        hex_rgb(paper.get('rgb'),'纸白实测')
        cal['paper']={'rgb':paper['rgb'].lower(),'measured':bool(paper.get('measured'))}
    patches=raw.get('patches')
    if not isinstance(patches,dict):
        raise Invalid('校准色块须为对象')
    keys=(*INKS,*(a+b for a,b in PAIRS))
    if any(k not in patches for k in keys):
        raise Invalid('校准色块不完整')
    for key in keys:
        item=patches[key]
        if not isinstance(item,dict):
            raise Invalid('校准色块须为对象')
        hex_rgb(item.get('rgb'),'校准色块')
        cal['patches'][key]={'rgb':item['rgb'].lower(),'measured':bool(item.get('measured'))}
    if not isinstance(raw.get('note',''),str) or len(raw.get('note',''))>300:
        raise Invalid('校准说明过长')
    cal['note']=str(raw.get('note',cal['note']))[:300]
    return cal


def build_proof_model(cal,fallback_paper):
    """Linear-light densities from measured patches; gaps stay on the fixed model.

    Returns paper reflectance, per-ink density (4x3) and pairwise residual
    density (6x3) so that paper*exp(-(density sums + residuals)) reproduces the
    measured solids and overprints. Approximations only fill missing inputs.
    """
    paper=linear(parse_color(cal['paper']['rgb'])) if cal['paper']['measured'] \
        else fallback_paper
    ink_density=np.zeros((4,3))
    for i,ink in enumerate(INKS):
        item=cal['patches'][ink]
        if item['measured']:
            refl=np.clip(linear(parse_color(item['rgb'])),REFLECT_EPS,np.maximum(paper,REFLECT_EPS))
            ink_density[i]=np.log(np.clip(paper/refl,1.,1/REFLECT_EPS))
        else:
            ink_density[i]=-np.log(np.clip(BASE_ABSORB[i],REFLECT_EPS,1))
    pair_residual=np.zeros((6,3))
    for j,(a,b) in enumerate(PAIRS):
        item=cal['patches'][a+b]
        if item['measured']:
            refl=np.clip(linear(parse_color(item['rgb'])),REFLECT_EPS,np.maximum(paper,REFLECT_EPS))
            observed=np.log(np.clip(paper/refl,1.,1/REFLECT_EPS))
            residual=observed-ink_density[INKS.index(a)]-ink_density[INKS.index(b)]
            pair_residual[j]=np.clip(residual,-RESIDUAL_LIMIT,RESIDUAL_LIMIT)
    return paper,ink_density,pair_residual


def calibration_summary(p):
    """Persistent description of which patches were measured vs approximated."""
    cal=p.get('calibration') or default_calibration(p.get('paper'))
    keys=('paper',*INKS,*(a+b for a,b in PAIRS))
    measured,approx=[],[]
    for key in keys:
        node=cal['paper'] if key=='paper' else cal['patches'][key]
        (measured if node.get('measured') else approx).append(key)
    return {'enabled':bool(cal.get('enabled')),'measured':measured,'approximated':approx,
            'method':'实测色块反推线性光密度，叠印残差成对校正；缺测项用固定透射率乘积近似',
            'disclaimer':'经验色块近似，不是ICC认证或合同打样',
            'note':cal.get('note','')}


def linear(a):
    a=np.asarray(a,dtype=float)
    return np.where(a<=.04045,a/12.92,((a+.055)/1.055)**2.4)


def srgb(a):
    a=np.clip(a,0,1)
    return np.where(a<=.0031308,a*12.92,1.055*a**(1/2.4)-.055)


def encode(image):
    stream=io.BytesIO()
    image.save(stream,'PNG')
    return stream.getvalue()


def decode(raw):
    if len(raw)>20*1024*1024:
        raise Invalid('源图最多20MB')
    try:
        im=Image.open(io.BytesIO(raw))
        if im.width*im.height>16_000_000:
            raise Invalid('源图最多1600万像素')
        im.load()
        return ImageOps.exif_transpose(im).convert('RGBA')
    except Invalid:
        raise
    except Exception as error:
        raise Invalid('无法解码源图') from error


def source_canvas(source,p):
    """Sample premultiplied linear color onto the physical page."""
    w,h=pixel_size(p)
    rgba=np.asarray(source,dtype=float)/255
    sh,sw=rgba.shape[:2]
    l,t,r,b=p['crop']
    cw,ch=(r-l)*sw,(b-t)*sh
    yy,xx=np.mgrid[:h,:w].astype(float)
    if p['fit']=='stretch':
        sx=l*sw+(xx+.5)*cw/w-.5
        sy=t*sh+(yy+.5)*ch/h-.5
        inside=np.ones((h,w),dtype=bool)
    else:
        scale=min(w/cw,h/ch) if p['fit']=='contain' else max(w/cw,h/ch)
        sx=l*sw+((xx+.5-w/2)/scale+cw/2)-.5
        sy=t*sh+((yy+.5-h/2)/scale+ch/2)-.5
        inside=(sx>=l*sw-.5)&(sx<r*sw-.5)&(sy>=t*sh-.5)&(sy<b*sh-.5)
    alpha=rgba[:,:,3]
    sampled_alpha=map_coordinates(alpha,[sy,sx],order=1,mode='nearest')*inside
    premult=linear(rgba[:,:,:3])*alpha[:,:,None]
    sampled=np.stack([map_coordinates(premult[:,:,c],[sy,sx],order=1,mode='nearest') for c in range(3)],2)
    sampled*=inside[:,:,None]
    paper=linear(parse_color(p['paper']))
    composed=sampled+paper*(1-sampled_alpha[:,:,None])
    return srgb(composed),sampled_alpha


def tone_rgb(rgb,p):
    lum=np.sum(linear(rgb)*np.array([.2126,.7152,.0722]),axis=2)
    adjusted=np.clip(lum[:,:,None]+(linear(rgb)-lum[:,:,None])*p['saturation'],0,1)
    return np.clip(srgb(adjusted)**p['gamma'],0,1)


def separate(rgb,p):
    """Simple subtractive GCR, with bounded total nominal ink."""
    rgb=tone_rgb(rgb,p)
    raw=1-rgb
    k=np.min(raw,axis=2)*p['black']
    chroma=np.maximum(raw-k[:,:,None],0)
    cmyk=np.concatenate([chroma,k[:,:,None]],axis=2)
    amount=cmyk.sum(axis=2)
    scale=np.minimum(1,p['ink_limit']/np.maximum(amount,1e-12))
    cmyk*=scale[:,:,None]
    cmyk*=np.asarray(p['enabled'],dtype=float)
    return np.clip(cmyk,0,1)


def dot_gain(tone,gain):
    """Symmetric midtone area expansion, endpoints unchanged."""
    return np.clip(tone+4*gain*tone*(1-tone),0,1)


def lattice_rank(x,y,angle,lpi,phase_x,phase_y):
    """Area-uniform diamond rank, in mm so DPI changes preserve phase."""
    theta=math.radians(angle)
    frequency=lpi/25.4
    u=(x*math.cos(theta)+y*math.sin(theta))*frequency+phase_x
    v=(-x*math.sin(theta)+y*math.cos(theta))*frequency+phase_y
    u=(u+.5)%1-.5
    v=(v+.5)%1-.5
    d=np.abs(u)+np.abs(v)
    return np.where(d<=.5,2*d*d,1-2*(1-d)**2)


def screen_plates(cmyk,p,origin=(0,0)):
    h,w=cmyk.shape[:2]
    yy,xx=np.mgrid[:h,:w].astype(float)
    x=(xx+.5+origin[0])*25.4/p['dpi']
    y=(yy+.5+origin[1])*25.4/p['dpi']
    out=[]
    for i in range(4):
        shiftx=p['offset_x'][i]
        shifty=p['offset_y'][i]
        coords=[yy-shifty*p['dpi']/25.4,xx-shiftx*p['dpi']/25.4]
        coverage=map_coordinates(cmyk[:,:,i],coords,order=1,mode='constant',cval=0)
        effective=dot_gain(coverage,p['gain'])
        if p['screened']:
            rank=lattice_rank(x-shiftx,y-shifty,p['angles'][i],p['lpi'],p['phase_x'][i],p['phase_y'][i])
            plate=(effective>=rank).astype(float)
        else:
            plate=effective
        out.append(plate)
    return np.stack(out,axis=2)


def proof(plates,p):
    """Approximate subtractive RGB proof calibrated from measured color patches.

    Calibration only changes this view: separations, nominal ink amounts and
    screened plates are computed independently and never altered here. With no
    measured patches the result is the fixed transmittance product model.
    """
    paper_setting=linear(parse_color(p['paper']))
    cal=p.get('calibration') or default_calibration(p['paper'])
    if not cal.get('enabled'):
        trans=np.clip(BASE_ABSORB,1e-4,1)
        density=np.einsum('hwi,ic->hwc',plates,np.log(trans))*p['proof_density']
        return srgb(paper_setting*np.exp(density))
    paper,ink_density,pair_residual=build_proof_model(cal,paper_setting)
    density=np.einsum('hwi,ic->hwc',plates,ink_density)
    for j,(a,b) in enumerate(PAIRS):
        ia,ib=INKS.index(a),INKS.index(b)
        density+=(plates[:,:,ia]*plates[:,:,ib])[:,:,None]*pair_residual[j]
    return srgb(np.clip(paper*np.exp(-np.maximum(density,0)),0,1))


def image(array):
    return Image.fromarray(np.rint(np.clip(array,0,1)*255).astype('uint8'))


def render(source,p):
    p=settings(p)
    rgb,alpha=source_canvas(source,p)
    cmyk=separate(rgb,p)
    plates=screen_plates(cmyk,p)
    views={'source':image(rgb),'proof':image(proof(plates,p)),'continuous':image(proof(cmyk,p))}
    for i,name in enumerate(INKS):
        views[name]=image(1-plates[:,:,i])
        views[name+'-tone']=image(1-cmyk[:,:,i])
    return views,{'nominal':cmyk,'plates':plates,'alpha':alpha}


def statistics(arrays,p):
    cmyk=arrays['nominal']
    plates=arrays['plates']
    w,h=pixel_size(p)
    report={'size':[w,h],'millimeters':[p['width_mm'],p['height_mm']],
            'dpi':p['dpi'],'lpi':p['lpi'],'pixels_per_cell':p['dpi']/p['lpi'],
            'total_ink_max':float(cmyk.sum(axis=2).max()),
            'total_ink_mean':float(cmyk.sum(axis=2).mean()),
            'proof_calibration':calibration_summary(p),'channels':{}}
    for i,name in enumerate(INKS):
        tone=cmyk[:,:,i]
        plate=plates[:,:,i]
        report['channels'][name]={'mean_tone':float(tone.mean()),'area_coverage':float(plate.mean()),
                                 'min':float(tone.min()),'max':float(tone.max()),
                                 'histogram':np.histogram(tone,bins=16,range=(0,1))[0].tolist()}
    return report


def measurement(arrays,rect):
    if not isinstance(rect,list) or len(rect)!=4:
        raise Invalid('测量区域须为四项归一化坐标')
    l,t,r,b=[finite(v,'测量区域',0,1) for v in rect]
    if not l<r or not t<b:
        raise Invalid('测量区域为空')
    h,w=arrays['nominal'].shape[:2]
    x0,x1=max(0,int(l*w)),min(w,math.ceil(r*w))
    y0,y1=max(0,int(t*h)),min(h,math.ceil(b*h))
    nominal=arrays['nominal'][y0:y1,x0:x1]
    plates=arrays['plates'][y0:y1,x0:x1]
    return {'rect':[l,t,r,b],'pixel_rect':[x0,y0,x1,y1],
            'nominal_mean':nominal.mean(axis=(0,1)).tolist(),
            'coverage':plates.mean(axis=(0,1)).tolist(),'pixel_count':int(nominal.shape[0]*nominal.shape[1])}
