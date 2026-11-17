"""Raster plate preparation in physical page coordinates."""
import io
import math
import hashlib
import numpy as np
from PIL import Image, ImageOps
from scipy.ndimage import (map_coordinates,binary_dilation,binary_erosion,
                           convolve)
from image_lab.validation import Invalid

INKS=('C','M','Y','K')
DEFAULTS={
    'width_mm':100.,'height_mm':75.,'dpi':96.,'lpi':18.,
    'black':.7,'ink_limit':2.8,'gamma':1.,'saturation':1.,
    'paper':'#fff8e8','gain':.12,'proof_density':1.,'trap_mm':0.,
    'angles':[15.,75.,0.,45.],'phase_x':[0.,0.,0.,0.],
    'phase_y':[0.,0.,0.,0.],'offset_x':[0.,0.,0.,0.],
    'offset_y':[0.,0.,0.,0.],'enabled':[True,True,True,True],
    'crop':[0.,0.,1.,1.],'fit':'contain','screened':True,
}
# Neutral-density style ink weights fix the spread direction: Y<C<M<K,
# so the lighter plate always grows into the darker neighbor.
TRAP_DENSITY=np.array([.61,.93,.06,1.64])
TRAP_COLORS=((0,150,175),(215,45,125),(245,205,25),(45,45,45))
TRAP_EPS=1e-6

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
            'saturation':(0,2),'gain':(-.3,.5),'proof_density':(.2,2.5),
            'trap_mm':(0,2)}
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


def _disk(radius):
    r=int(math.ceil(radius-1e-9))
    yy,xx=np.mgrid[-r:r+1,-r:r+1]
    return (xx*xx+yy*yy<=max(radius,.5)**2).astype(float)


def trap_plates(plates,cmyk,p):
    """Directional spread trap, width trap_mm in physical page units.

    A lighter ink plate grows by the trap width into adjacent darker ink only;
    plate edges meeting bare paper never spread, so counters (letter holes) and
    paper white stay open. Dark solid components thinner than two trap widths
    are treated as hairlines and refuse the trap, keeping thin strokes intact.
    Geometry/direction tests use the continuous-tone separation (halftone dots
    are disconnected and cannot describe feature thickness); radius is
    mm*dpi/25.4, so a fixed trap width survives DPI changes.
    Returns (trapped plates, per-ink added-band mask, spread-pixel matrix).
    """
    width=p['trap_mm']
    binary=p['screened']
    trapped=plates.copy()
    h,w=plates.shape[:2]
    added=np.zeros_like(plates)
    spread=np.zeros((4,4),dtype=int)
    if width<=0:
        return trapped,added,spread
    radius=width*p['dpi']/25.4
    foot=_disk(radius)
    solid=cmyk>=.5
    any_solid=solid.any(axis=2)
    # Pixels within the trap reach of bare paper cannot receive a trap:
    # ink-to-paper edges are gaps against paper, not another ink, and the
    # guard keeps enclosed letter counters open. Resampled paper has no solid
    # channel, so the any_solid rule still admits genuine ink-to-ink seams.
    paper_near=binary_dilation(~any_solid,structure=foot)
    darkness=np.clip(cmyk@TRAP_DENSITY,0,None)
    # Hairline guard: erode each solid ink by the trap radius and dilate the
    # surviving core back. A stroke thinner than 2*radius loses its core and
    # stays unmarked, so hairlines and thin text strokes refuse the trap;
    # broad fields are restored edge-to-edge even when their anti-aliased
    # seam strip itself is only a few pixels wide.
    wide=np.stack([binary_dilation(binary_erosion(solid[:,:,j],structure=foot,
                                                   border_value=0),structure=foot)
                   for j in range(4)],axis=2)
    for i in range(4):
        if not p['enabled'][i]:
            continue
        source_ink=solid[:,:,i]
        # Candidate band: growth reach of this plate, excluding its own ink,
        # pixels with no solid neighbor ink, and pure-paper pixels. The
        # any_solid rule keeps traps off bare paper even across anti-aliased
        # boundaries, so enclosed paper counters (letter holes) never fill.
        other_ink=np.delete(solid,i,axis=2).any(axis=2)
        band=(binary_dilation(source_ink,structure=foot)&~source_ink
              &any_solid&other_ink&~paper_near)
        # Hairline guard: every solid ink under the band must be a wide feature.
        allowed=np.delete(wide,i,axis=2).any(axis=2)
        band&=allowed
        # Lighter-into-darker: mean source-side darkness must sit below the
        # darkness currently printed at the candidate pixel.
        weight=cmyk[:,:,i]*source_ink
        local_weight=convolve(weight,foot,mode='constant',cval=0)
        src_dark=convolve(darkness*weight,foot,mode='constant',cval=0)/np.maximum(local_weight,1e-12)
        band&=(src_dark+TRAP_EPS<darkness)
        if binary:
            trapped[band,i]=1.
        else:
            tone=convolve(plates[:,:,i]*source_ink,foot,mode='constant',cval=0)/np.maximum(local_weight,1e-12)
            trapped[band,i]=np.maximum(trapped[band,i],np.clip(tone[band],0,1))
        added[band,i]=1.
        for j in range(4):
            if j!=i:
                spread[i,j]=int(np.count_nonzero(band&solid[:,:,j]))
    return trapped,added,spread


def trap_band(plates,added):
    """RGB change map: gray original inks, process colors where each plate grew."""
    base=(1-np.clip(plates.sum(axis=2),0,1)*.7)
    view=np.repeat(base[:,:,None],3,axis=2)
    for i in range(4):
        view[added[:,:,i]>.5]=np.array(TRAP_COLORS[i],dtype=float)/255
    return view


def proof(plates,p):
    """Approximate subtractive RGB proof; no measured press profile claim."""
    paper=linear(parse_color(p['paper']))
    absorb=np.array([[.04,.83,.88],[.84,.06,.80],[.80,.84,.03],[.08,.08,.08]])
    trans=np.clip(absorb,1e-4,1)
    logt=np.log(trans)
    density=np.einsum('hwi,ic->hwc',plates,logt)*p['proof_density']
    return srgb(paper*np.exp(density))


def image(array):
    return Image.fromarray(np.rint(np.clip(array,0,1)*255).astype('uint8'))


def render(source,p):
    p=settings(p)
    rgb,alpha=source_canvas(source,p)
    cmyk=separate(rgb,p)
    raw=screen_plates(cmyk,p)
    plates,added,spread=trap_plates(raw,cmyk,p)
    views={'source':image(rgb),'proof':image(proof(plates,p)),
           'continuous':image(proof(cmyk,p)),'proof-raw':image(proof(raw,p)),
           'trap':image(trap_band(plates,added))}
    for i,name in enumerate(INKS):
        views[name]=image(1-plates[:,:,i])
        views[name+'-raw']=image(1-raw[:,:,i])
        views[name+'-tone']=image(1-cmyk[:,:,i])
    return views,{'nominal':cmyk,'plates':plates,'raw':raw,'added':added,
                  'spread':spread,'alpha':alpha}


def trap_report(arrays,p):
    added=arrays['added']
    return {'trap_mm':p['trap_mm'],'radius_px':p['trap_mm']*p['dpi']/25.4,
            'rule':'浅色墨版向相邻深色墨版搭接；不向纸面扩张；窄于两倍陷印宽度的细线拒绝搭接',
            'added_pixels':{INKS[i]:int(np.count_nonzero(added[:,:,i])) for i in range(4)},
            'spread':{INKS[i]:{INKS[j]:int(arrays['spread'][i,j]) for j in range(4) if j!=i}
                      for i in range(4)}}


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
    report['trap']=trap_report(arrays,p)
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
