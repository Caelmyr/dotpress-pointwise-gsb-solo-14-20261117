"""Raster plate preparation in physical page coordinates.

Rendering streams the page in row bands.  Each band is addressed in the same
absolute page coordinates (mm for the screen lattice, page pixels for
registration) and reads the complete-page separation, so splitting a page
never restarts a screen grid or drops a bilinear interpolation neighbour.
Band sizes only bound transient buffers; they do not change the result.
"""
import io
import math
import numpy as np
from PIL import Image, ImageOps
from scipy.ndimage import map_coordinates
from image_lab.validation import Invalid

INKS=('C','M','Y','K')
DEFAULTS={
    'width_mm':100.,'height_mm':75.,'dpi':96.,'lpi':18.,
    'black':.7,'ink_limit':2.8,'gamma':1.,'saturation':1.,
    'paper':'#fff8e8','gain':.12,'proof_density':1.,
    'angles':[15.,75.,0.,45.],'phase_x':[0.,0.,0.,0.],
    'phase_y':[0.,0.,0.,0.],'offset_x':[0.,0.,0.,0.],
    'offset_y':[0.,0.,0.,0.],'enabled':[True,True,True,True],
    'crop':[0.,0.,1.,1.],'fit':'contain','screened':True,
}
# Page rows per screening band: bounds the plate/proof float temporaries.
BAND_ROWS=64
# Source pixels converted per resampling band: bounds the float source window.
SOURCE_BAND_PIXELS=900_000
# Ceiling on page rows per band so very wide pages stay bounded too.
SOURCE_BAND_ROWS=192


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
    w,h=pixel_size(p)
    if max(w,h)>1200 or w*h>1200**2:
        raise Invalid('当前页面像素预算为1200×1200，请降低DPI或尺寸')
    if p['lpi']>p['dpi']/2:
        raise Invalid('网线频率不能超过DPI的一半')
    return p


def pixel_size(p):
    return (max(1,round(p['width_mm']/25.4*p['dpi'])),
            max(1,round(p['height_mm']/25.4*p['dpi'])))


def parse_color(text):
    if not isinstance(text,str) or len(text)!=7 or text[0]!='#':
        raise Invalid('纸色须为#RRGGBB')
    try:
        return np.array([int(text[i:i+2],16) for i in (1,3,5)],dtype=float)/255
    except ValueError as error:
        raise Invalid('纸色无效') from error


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


def _rows(count):
    for y0 in range(0,count,BAND_ROWS):
        yield y0,min(count,y0+BAND_ROWS)


def _source_window(coord,limit):
    """Source pixels needed by one band's bilinear samples, plus a margin.

    The window is clamped to the source but never empty, so bands that fall
    entirely outside the photo still resample against the nearest edge exactly
    as the complete-page sampler would.
    """
    lo=int(math.floor(float(np.min(coord))))-1
    hi=int(math.floor(float(np.max(coord))))+2
    lo=min(max(lo,0),limit-1)
    hi=min(max(hi,lo+1),limit)
    return lo,hi


def _fit_scale(p,w,h,sw,sh):
    """Scale and crop extent shared by the source sampler and band planner."""
    l,t,r,b=p['crop']
    cw,ch=(r-l)*sw,(b-t)*sh
    if p['fit']=='stretch':
        return None,cw,ch
    scale=min(w/cw,h/ch) if p['fit']=='contain' else max(w/cw,h/ch)
    return scale,cw,ch


def _fill_bands(p,sw,sh):
    """Page row bands sized to keep the float source window bounded.

    A band resamples the source columns its output width maps to and the source
    rows its output rows map to, so the window size is derived from the fit
    scale: heavy minification makes each page row cover many source rows and
    therefore shortens the band.  Band choice never changes coordinates.
    """
    w,h=pixel_size(p)
    scale,cw,ch=_fit_scale(p,w,h,sw,sh)
    if scale is None:
        columns=min(sw,cw+2.)
        rows_per_row=max(1.,ch/h)
    else:
        columns=min(sw,max(w/cw,h/ch)/scale*w+2.)
        rows_per_row=max(1.,1./scale)
    budget=SOURCE_BAND_PIXELS/max(1.,columns*rows_per_row)
    band=max(1,min(SOURCE_BAND_ROWS,h,int(budget)))
    for y0 in range(0,h,band):
        yield y0,min(h,y0+band)


def source_canvas_band(source,p,y0,y1):
    """Sample premultiplied linear colour for page rows [y0,y1).

    The mapping, ``inside`` mask, paper compositing and ``srgb`` transform are
    the complete-page ones evaluated on a row slice, so neighbouring bands are
    continuous.  Only the source crop this band can touch is converted to
    float, and source channels are converted one at a time.
    """
    w,h=pixel_size(p)
    sh,sw=source.height,source.width
    l,t,r,b=p['crop']
    cw,ch=(r-l)*sw,(b-t)*sh
    yy=np.arange(y0,y1,dtype=float)[:,None]
    xx=np.arange(w,dtype=float)[None,:]
    if p['fit']=='stretch':
        sx=l*sw+(xx+.5)*cw/w-.5
        sy=t*sh+(yy+.5)*ch/h-.5
        inside=np.ones((y1-y0,w),dtype=bool)
    else:
        scale=min(w/cw,h/ch) if p['fit']=='contain' else max(w/cw,h/ch)
        sx=l*sw+((xx+.5-w/2)/scale+cw/2)-.5
        sy=t*sh+((yy+.5-h/2)/scale+ch/2)-.5
        inside=(sx>=l*sw-.5)&(sx<r*sw-.5)&(sy>=t*sh-.5)&(sy<b*sh-.5)
    c0,c1=_source_window(sx,sw)
    r0,r1=_source_window(sy,sh)
    crop=np.asarray(source.crop((c0,r0,c1,r1)))
    alpha=np.asarray(crop[:,:,3],dtype=float)/255
    coords=np.broadcast_arrays(sy-r0,sx-c0)
    sampled_alpha=map_coordinates(alpha,coords,order=1,mode='nearest')*inside
    sampled=np.empty((y1-y0,w,3),dtype=float)
    for c in range(3):
        channel=np.asarray(crop[:,:,c],dtype=float)/255
        premult=linear(channel)*alpha
        del channel
        sampled[:,:,c]=map_coordinates(premult,coords,order=1,mode='nearest')
        del premult
    del alpha,crop
    sampled*=inside[:,:,None]
    paper=linear(parse_color(p['paper']))
    sampled+=(paper*(1-sampled_alpha[:,:,None]))
    return srgb(sampled),sampled_alpha


def source_canvas(source,p):
    """Sample premultiplied linear colour onto the physical page."""
    w,h=pixel_size(p)
    sh,sw=source.height,source.width
    parts=[source_canvas_band(source,p,y0,y1) for y0,y1 in _fill_bands(p,sw,sh)]
    rgb=np.concatenate([part[0] for part in parts],axis=0)
    alpha=np.concatenate([part[1] for part in parts],axis=0)
    return rgb,alpha


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


def screen_band(cmyk,y0,y1,p,row0=0,origin=(0,0)):
    """Screen page rows [y0,y1) from a nominal-ink array.

    ``cmyk`` holds page rows starting at ``row0``; ``origin`` shifts the
    physical page origin in pixels.  Coordinates are absolute page
    coordinates, and ``mode='constant'`` applies at the page edge, so the
    result does not depend on where a band starts or ends.
    """
    w=cmyk.shape[1]
    yy=np.arange(y0,y1,dtype=float)[:,None]
    xx=np.arange(w,dtype=float)[None,:]
    yy,xx=np.broadcast_arrays(yy,xx)
    out=[]
    for i in range(4):
        shiftx=p['offset_x'][i]
        shifty=p['offset_y'][i]
        coords=[yy-row0-shifty*p['dpi']/25.4,xx-shiftx*p['dpi']/25.4]
        coverage=map_coordinates(cmyk[:,:,i],coords,order=1,mode='constant',cval=0)
        effective=dot_gain(coverage,p['gain'])
        if p['screened']:
            x=(xx+.5+origin[0])*25.4/p['dpi']
            y=(yy+.5+origin[1])*25.4/p['dpi']
            rank=lattice_rank(x-shiftx,y-shifty,p['angles'][i],p['lpi'],p['phase_x'][i],p['phase_y'][i])
            plate=(effective>=rank).astype(float)
        else:
            plate=effective
        out.append(plate)
    return np.stack(out,axis=2)


def screen_plates(cmyk,p,origin=(0,0)):
    h,w=cmyk.shape[:2]
    return np.concatenate([screen_band(cmyk,y0,y1,p,origin=origin) for y0,y1 in _rows(h)],axis=0)


def proof(plates,p):
    """Approximate subtractive RGB proof; no measured press profile claim."""
    paper=linear(parse_color(p['paper']))
    absorb=np.array([[.04,.83,.88],[.84,.06,.80],[.80,.84,.03],[.08,.08,.08]])
    trans=np.clip(absorb,1e-4,1)
    logt=np.log(trans)
    density=np.einsum('hwi,ic->hwc',plates,logt)*p['proof_density']
    return srgb(paper*np.exp(density))


def _uint8(array):
    """Quantise a unit-float page array to the 8-bit channel PNG encoding."""
    return np.rint(np.clip(array,0,1)*255).astype('uint8')


def image(array):
    return Image.fromarray(_uint8(array))


def _region(rect,w,h):
    if not isinstance(rect,list) or len(rect)!=4:
        raise Invalid('测量区域须为四项归一化坐标')
    l,t,r,b=[finite(v,'测量区域',0,1) for v in rect]
    if not l<r or not t<b:
        raise Invalid('测量区域为空')
    return l,t,r,b,max(0,int(l*w)),max(0,int(t*h)),min(w,math.ceil(r*w)),min(h,math.ceil(b*h))


def _views(p):
    w,h=pixel_size(p)
    buffers={'source':np.empty((h,w,3),dtype='uint8'),
             'proof':np.empty((h,w,3),dtype='uint8'),
             'continuous':np.empty((h,w,3),dtype='uint8')}
    for name in INKS:
        buffers[name]=np.empty((h,w),dtype='uint8')
        buffers[name+'-tone']=np.empty((h,w),dtype='uint8')
    return buffers


def render_page(source,p,rect=None,views=True):
    """Render one complete page as streaming bands.

    Returns ``(views, report, measurement)``.  ``views`` maps each deliverable
    channel to a PIL image, ``report`` is the whole-page coverage report and
    ``measurement`` is the optional region measurement.

    Source resampling, separation, screening, both proofs and PNG encoding all
    advance in row bands, so no full-resolution proof, intermediate colour or
    premultiplied source stack is ever alive.  The nominal-ink page and its
    screened plates are the only full-page float arrays; the report and region
    measurement reduce them with the same whole-page operations an undivided
    render uses, so all three outputs describe the same complete render.
    """
    p=settings(p)
    w,h=pixel_size(p)
    sh,sw=source.height,source.width
    cmyk=np.empty((h,w,4),dtype=float)
    buffers=_views(p) if views else None
    for y0,y1 in _fill_bands(p,sw,sh):
        rgb,alpha=source_canvas_band(source,p,y0,y1)
        cmyk[y0:y1]=separate(rgb,p)
        if buffers is not None:
            buffers['source'][y0:y1]=_uint8(rgb)
        del rgb,alpha
    plates=np.empty((h,w,4),dtype=float)
    for y0,y1 in _rows(h):
        band=screen_band(cmyk,y0,y1,p)
        plates[y0:y1]=band
        if buffers is not None:
            buffers['proof'][y0:y1]=_uint8(proof(band,p))
            buffers['continuous'][y0:y1]=_uint8(proof(cmyk[y0:y1],p))
            for i,name in enumerate(INKS):
                buffers[name][y0:y1]=_uint8(1-band[:,:,i])
                buffers[name+'-tone'][y0:y1]=_uint8(1-cmyk[y0:y1,:,i])
        del band
    arrays={'nominal':cmyk,'plates':plates}
    report=statistics(arrays,p)
    measure=measurement(arrays,rect) if rect is not None else None
    result={name:Image.fromarray(array) for name,array in buffers.items()} if buffers is not None else None
    return result,report,measure


def render(source,p):
    """Compatibility path that also returns the full nominal/plate arrays.

    Source resampling, separation, screening and view encoding all stream in
    bands, so the only full-page buffers are the arrays this signature
    promises to return.  Production endpoints use :func:`render_page`.
    """
    p=settings(p)
    w,h=pixel_size(p)
    sh,sw=source.height,source.width
    cmyk=np.empty((h,w,4),dtype=float)
    alpha=np.empty((h,w),dtype=float)
    views=_views(p)
    for y0,y1 in _fill_bands(p,sw,sh):
        rgb,a=source_canvas_band(source,p,y0,y1)
        cmyk[y0:y1]=separate(rgb,p)
        alpha[y0:y1]=a
        views['source'][y0:y1]=_uint8(rgb)
        del rgb,a
    plates=np.empty((h,w,4),dtype=float)
    for x0,x1 in _rows(h):
        band=screen_band(cmyk,x0,x1,p)
        plates[x0:x1]=band
        views['proof'][x0:x1]=_uint8(proof(band,p))
        views['continuous'][x0:x1]=_uint8(proof(cmyk[x0:x1],p))
        for i,name in enumerate(INKS):
            views[name][x0:x1]=_uint8(1-band[:,:,i])
            views[name+'-tone'][x0:x1]=_uint8(1-cmyk[x0:x1,:,i])
        del band
    return {name:Image.fromarray(array) for name,array in views.items()},{'nominal':cmyk,'plates':plates,'alpha':alpha}


def statistics(arrays,p):
    cmyk=arrays['nominal']
    plates=arrays['plates']
    w,h=pixel_size(p)
    report={'size':[w,h],'millimeters':[p['width_mm'],p['height_mm']],
            'dpi':p['dpi'],'lpi':p['lpi'],'pixels_per_cell':p['dpi']/p['lpi'],
            'total_ink_max':float(cmyk.sum(axis=2).max()),
            'total_ink_mean':float(cmyk.sum(axis=2).mean()),
            'channels':{}}
    for i,name in enumerate(INKS):
        tone=cmyk[:,:,i]
        plate=plates[:,:,i]
        report['channels'][name]={'mean_tone':float(tone.mean()),'area_coverage':float(plate.mean()),
                                 'min':float(tone.min()),'max':float(tone.max()),
                                 'histogram':np.histogram(tone,bins=16,range=(0,1))[0].tolist()}
    return report


def measurement(arrays,rect):
    l,t,r,b,x0,y0,x1,y1=_region(rect,arrays['nominal'].shape[1],arrays['nominal'].shape[0])
    nominal=arrays['nominal'][y0:y1,x0:x1]
    plates=arrays['plates'][y0:y1,x0:x1]
    return {'rect':[l,t,r,b],'pixel_rect':[x0,y0,x1,y1],
            'nominal_mean':nominal.mean(axis=(0,1)).tolist(),
            'coverage':plates.mean(axis=(0,1)).tolist(),'pixel_count':int(nominal.shape[0]*nominal.shape[1])}
