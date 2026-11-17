"""Raster plate preparation in physical page coordinates."""
import io
import math
import itertools
import hashlib
import numpy as np
from PIL import Image, ImageOps
from scipy.ndimage import map_coordinates
from image_lab.validation import Invalid

INKS=('C','M','Y','K')
SPOT_MIN,SPOT_MAX=2,6
SPOT_ANGLES=(15.,75.,45.,0.,30.,60.)
SPOT_PRESET=(
    {'name':'深青','color':'#0E6E70','angle':15.},
    {'name':'朱红','color':'#C94F3D','angle':75.},
    {'name':'墨黑','color':'#202024','angle':45.},
    {'name':'土黄','color':'#D9A441','angle':0.},
)
ERROR_TOLERANCE=.02
REFINE_TARGET_ROWS=200_000
DEFAULTS={
    'width_mm':100.,'height_mm':75.,'dpi':96.,'lpi':18.,
    'black':.7,'ink_limit':2.8,'gamma':1.,'saturation':1.,
    'paper':'#fff8e8','gain':.12,'proof_density':1.,
    'angles':[15.,75.,0.,45.],'phase_x':[0.,0.,0.,0.],
    'phase_y':[0.,0.,0.,0.],'offset_x':[0.,0.,0.,0.],
    'offset_y':[0.,0.,0.,0.],'enabled':[True,True,True,True],
    'crop':[0.,0.,1.,1.],'fit':'contain','screened':True,
    'separation':'cmyk','spot_inks':[],'error_scale':.25,
}

def finite(value,label,low,high):
    try:
        number=float(value)
    except (ValueError,TypeError):
        raise Invalid(label+'必须为数字')
    if not math.isfinite(number) or not low<=number<=high:
        raise Invalid(label+f'须在{low}至{high}之间')
    return number


def hex_color(value,label='颜色'):
    if not isinstance(value,str) or len(value)!=7 or value[0]!='#':
        raise Invalid(label+'须为#RRGGBB')
    try:
        int(value[1:],16)
    except ValueError as error:
        raise Invalid(label+'无效') from error
    return '#'+value[1:].lower()


def spot_ink_list(raw):
    """Two to six named, uniquely coloured process inks."""
    if not isinstance(raw,list) or not SPOT_MIN<=len(raw)<=SPOT_MAX:
        raise Invalid(f'专色方案须为{SPOT_MIN}至{SPOT_MAX}罐墨')
    inks=[]; names=set(); colors=set()
    for index,item in enumerate(raw,1):
        if not isinstance(item,dict):
            raise Invalid(f'第{index}罐墨须为对象')
        name=item.get('name')
        if not isinstance(name,str) or not name.strip() or len(name.strip())>40:
            raise Invalid(f'第{index}罐墨须有1至40字名称')
        name=name.strip()
        if name in names:
            raise Invalid('专色墨名称不得重复：'+name)
        color=hex_color(item.get('color'),f'第{index}罐墨颜色')
        if color in colors:
            raise Invalid('专色墨颜色不得重复：'+color)
        names.add(name); colors.add(color)
        inks.append({'name':name,'color':color})
    return inks


def _channel_count(p):
    return len(p['spot_inks']) if p['separation']=='spot' else len(INKS)


def _default_channel(key,index):
    if key=='angles':
        return SPOT_ANGLES[index%len(SPOT_ANGLES)]
    return True if key=='enabled' else 0.


def _resize_channel(values,key,count):
    values=list(values)[:count]
    values.extend(_default_channel(key,index) for index in range(len(values),count))
    return values


def settings(raw=None,strict=False):
    raw=raw or {}
    if not isinstance(raw,dict):
        raise Invalid('印版参数须为对象')
    p={**DEFAULTS,**raw}
    if p.get('separation') not in ('cmyk','spot'):
        raise Invalid('分色方式无效')
    if p['separation']=='spot':
        p['spot_inks']=spot_ink_list(p.get('spot_inks'))
    else:
        p['spot_inks']=[]
    count=_channel_count(p)
    ranges={'width_mm':(10,300),'height_mm':(10,300),'dpi':(36,300),
            'lpi':(3,80),'black':(0,1),'ink_limit':(1,4),'gamma':(.2,4),
            'saturation':(0,2),'gain':(-.3,.5),'proof_density':(.2,2.5),
            'error_scale':(.02,1)}
    for key,(lo,hi) in ranges.items():
        p[key]=finite(p[key],key,lo,hi)
    for key in ('angles','phase_x','phase_y','offset_x','offset_y'):
        if not isinstance(p[key],list):
            raise Invalid(key+'须为通道数组')
        if len(p[key])>count:
            if strict:
                raise Invalid(f'{key}须为{count}通道数组')
            p[key]=_resize_channel(p[key],key,count)
        lo,hi=(-180,180) if key=='angles' else (-20,20)
        p[key]=[finite(v,key,lo,hi) for v in p[key]]
        if len(p[key])<count:
            p[key]=_resize_channel(p[key],key,count)
    if not isinstance(p['enabled'],list):
        raise Invalid('启用通道须为数组')
    if len(p['enabled'])>count:
        if strict:
            raise Invalid(f'启用通道须为{count}项')
        p['enabled']=_resize_channel(p['enabled'],'enabled',count)
    p['enabled']=[bool(v) for v in p['enabled']]
    while len(p['enabled'])<count:
        p['enabled'].append(True)
    if p['separation']=='spot' and not any(p['enabled']):
        raise Invalid('专色方案至少启用一罐墨')
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


def ink_density(p):
    """Per-ink optical density at full coverage: -ln(reflectance) of the ink."""
    return -np.log(np.clip(ink_reflectance(p),1e-4,1))


def ink_reflectance(p):
    if p['separation']=='spot':
        return linear(np.asarray([parse_color(ink['color']) for ink in p['spot_inks']]))
    return np.asarray([[.04,.83,.88],[.84,.06,.80],[.80,.84,.03],[.08,.08,.08]])


def ink_channels(p):
    """Stable file/URL keys plus the human name and swatch for every plate."""
    if p['separation']=='spot':
        return [{'key':f'ink{index+1:02d}','name':ink['name'],'color':ink['color'],
                 'angle':p['angles'][index]}
                for index,ink in enumerate(p['spot_inks'])]
    swatches=('#e8ebe6','#c9dcc8','#f0e2a8','#3a3a38')
    return [{'key':name,'name':name,'color':swatches[index],'angle':p['angles'][index]}
            for index,name in enumerate(INKS)]


def _ink_supports(M,maximum=3):
    """One least-squares table per possible ink support, in both forms.

    A target colour has three components, so an optimum needs at most three
    inks that are strictly between zero and full coverage. Each support is
    prepared twice: unconstrained, and with every ink of the support at the top
    of the remaining budget. Together they cover interior and budget-boundary
    optima, and a small support can always satisfy the box, so every pixel gets
    an answer.
    """
    supports=[]
    for size in range(1,min(M.shape[0],maximum)+1):
        for support in itertools.combinations(range(M.shape[0]),size):
            inks=M[list(support)].astype(np.float64)
            gram=inks@inks.T
            bordered=np.zeros((size+1,size+1))
            bordered[:-1,:-1]=gram;bordered[:-1,-1]=1.;bordered[-1,:-1]=1.
            capped=np.linalg.pinv(bordered)
            supports.append({'support':support,
                             'inks':np.ascontiguousarray(inks.astype(np.float32)),
                             'free':np.ascontiguousarray((np.linalg.pinv(gram)@inks).T.astype(np.float32)),
                             'capped':np.ascontiguousarray((capped[:-1,:-1]@inks).T.astype(np.float32)),
                             'last':capped[:-1,-1].astype(np.float32)})
    return supports


def _within_box(a,tolerance=1e-5):
    """Row-wise 0..1 check that touches each ink column once."""
    feasible=(a[:,0]>=-tolerance)&(a[:,0]<=1+tolerance)
    for column in range(1,a.shape[1]):
        feasible&=(a[:,column]>=-tolerance)&(a[:,column]<=1+tolerance)
    return feasible


def _offer(amounts,best,rows,pin,coeff,support,error,feasible):
    """Adopt a candidate fit wherever it is feasible and better than the rest."""
    better=feasible&(error<best[rows]-1e-9)
    if not better.any():
        return
    chosen=rows[better]
    amounts[chosen]=0
    if pin:
        amounts[np.ix_(chosen,pin)]=1
    for column,ink in enumerate(support):
        amounts[chosen,ink]=np.clip(coeff[better,column],0,1)
    best[chosen]=error[better]


def _fit_inks(target,M,limit,row_budget=None,chunk=120_000):
    """Bounded least-squares ink amounts for every target colour.

    Stage one solves every support of up to three inks, which reproduces a
    colour exactly whenever the palette can reach it. Stage two revisits only
    pixels that still carry a residual and repeats that search with inks held
    at full coverage, where unreachable colours settle on the box boundary; one
    and then two full inks cover every active set the budget allows. So the
    cost tracks the genuinely unrepresentable area instead of the whole page.
    The shared budget stops a pathological page from spending unbounded time;
    any pixels left beyond it keep the best fit found so far.
    """
    supports=_ink_supports(M)
    count,n=target.shape[0],M.shape[0]
    amounts=np.zeros((count,n),np.float32)
    best=np.full(count,np.inf,np.float32)
    for start in range(0,count,chunk):
        rows=np.arange(start,min(count,start+chunk))
        sub=target[rows]
        for entry in supports:
            inks=entry['inks']
            coeff=sub@entry['free']
            difference=coeff@inks-sub
            error=np.einsum('ij,ij->i',difference,difference)
            feasible=_within_box(coeff)&(coeff.sum(axis=1)<=limit+1e-5)
            _offer(amounts,best,rows,(),coeff,entry['support'],error,feasible)
            coeff=sub@entry['capped']+entry['last']*limit
            difference=coeff@inks-sub
            error=np.einsum('ij,ij->i',difference,difference)
            _offer(amounts,best,rows,(),coeff,entry['support'],error,_within_box(coeff))
        open_rows=rows[best[rows]>1e-9]
        if row_budget is not None:
            # Spread the refinement budget evenly over the page so no band of
            # the image is favoured; every chunk gets its proportional share.
            share=max(1,int(row_budget*rows.size/count)) if row_budget else 0
            open_rows=open_rows[:share]
        for depth in (1,2):
            if depth>limit or not open_rows.size:
                break
            for pin in itertools.combinations(range(n),depth):
                _refine(target,amounts,best,open_rows,pin,M,limit,supports)
    return amounts,best


def _refine(target,amounts,best,rows,pin,M,limit,supports):
    """Second pass for residual pixels with these inks held at full coverage."""
    base=M[list(pin)].sum(axis=0)
    shifted=np.ascontiguousarray(target[rows]-base)
    error=np.einsum('ij,ij->i',shifted,shifted)
    _offer(amounts,best,rows,pin,np.zeros((rows.size,0),np.float32),(),error,
           np.ones(rows.size,bool))
    for entry in supports:
        support=entry['support']
        if set(support)&set(pin):
            continue
        inks=entry['inks']
        coeff=shifted@entry['free']
        difference=coeff@inks-shifted
        error=np.einsum('ij,ij->i',difference,difference)
        feasible=_within_box(coeff)&(coeff.sum(axis=1)+len(pin)<=limit+1e-5)
        _offer(amounts,best,rows,pin,coeff,support,error,feasible)
        if limit-len(pin)<=len(support)+1e-6:
            coeff=shifted@entry['capped']+entry['last']*max(limit-len(pin),0.)
            difference=coeff@inks-shifted
            error=np.einsum('ij,ij->i',difference,difference)
            _offer(amounts,best,rows,pin,coeff,support,error,_within_box(coeff))


def separate_spot(rgb,p,row_limit=None):
    """Per-pixel ink amounts for the fixed palette, by bounded least squares.

    The target is the optical density the page colour requires. The solver fits
    ink amounts whose summed densities come closest to it, keeping every amount
    non-negative, at most full coverage, and the total within the ink limit.
    Where the palette cannot span the target the fit is kept and the leftover
    residual becomes the reported reconstruction error, never a fake match.
    Disabled inks stay at zero and take no part in the fit.
    """
    paper=linear(parse_color(p['paper']))
    tone=np.clip(tone_rgb(rgb,p),1e-4,1)
    target=np.clip(-np.log(np.clip(linear(tone)/paper,1e-4,1)),0,None)
    active=np.flatnonzero(p['enabled'])
    flat=target.reshape(-1,3).astype(np.float32)
    M=ink_density(p)[active].astype(np.float32)
    amounts=np.zeros((flat.shape[0],len(p['enabled'])),np.float32)
    budget=REFINE_TARGET_ROWS if row_limit is None else row_limit
    amounts[:,active]=_fit_inks(flat,M,p['ink_limit'],row_budget=budget)[0]
    return amounts.reshape(target.shape[:2]+(len(p['enabled']),)),target


def ink_error(amounts,target,p):
    """Per-pixel linear-reflectance distance between fit and requested colour."""
    paper=linear(parse_color(p['paper']))
    fit=amounts.reshape(-1,amounts.shape[2])@ink_density(p).astype(np.float32)
    wanted=np.exp(-target.reshape(-1,3))*paper
    produced=np.exp(-fit)*paper
    return np.sqrt(((produced-wanted)**2).mean(axis=1)).reshape(target.shape[:2])


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


def screen_plates(nominal,p,origin=(0,0)):
    h,w,count=nominal.shape
    yy,xx=np.mgrid[:h,:w].astype(float)
    x=(xx+.5+origin[0])*25.4/p['dpi']
    y=(yy+.5+origin[1])*25.4/p['dpi']
    out=[]
    for i in range(count):
        shiftx=p['offset_x'][i]
        shifty=p['offset_y'][i]
        coords=[yy-shifty*p['dpi']/25.4,xx-shiftx*p['dpi']/25.4]
        coverage=map_coordinates(nominal[:,:,i],coords,order=1,mode='constant',cval=0)
        effective=dot_gain(coverage,p['gain'])
        if p['screened']:
            rank=lattice_rank(x-shiftx,y-shifty,p['angles'][i],p['lpi'],p['phase_x'][i],p['phase_y'][i])
            plate=(effective>=rank).astype(float)
        else:
            plate=effective
        out.append(plate)
    return np.stack(out,axis=2)


def proof(plates,p):
    """Approximate subtractive RGB proof; no measured press profile claim."""
    paper=linear(parse_color(p['paper']))
    logt=np.log(np.clip(ink_reflectance(p),1e-4,1))
    density=np.einsum('hwi,ic->hwc',plates,logt)*p['proof_density']
    return srgb(paper*np.exp(density))


def error_image(values,p):
    """Warm heat map: pale is exact, deep red is far outside the palette."""
    value=np.clip(values/p['error_scale'],0,1)[:,:,None]
    low=np.array([.93,.93,.88]);high=np.array([.62,.09,.05])
    return low+(high-low)*value**.7


def image(array):
    return Image.fromarray(np.rint(np.clip(array,0,1)*255).astype('uint8'))


def render(source,p):
    """One separation drives every view, statistic and download of this result."""
    p=settings(p)
    rgb,alpha=source_canvas(source,p)
    if p['separation']=='spot':
        nominal,target=separate_spot(rgb,p)
        error=ink_error(nominal,target,p)
    else:
        nominal=separate(rgb,p)
        target=None
        error=None
    plates=screen_plates(nominal,p)
    views={'source':image(rgb),'proof':image(proof(plates,p)),'continuous':image(proof(nominal,p))}
    for index,channel in enumerate(ink_channels(p)):
        views[channel['key']]=image(1-plates[:,:,index])
        views[channel['key']+'-tone']=image(1-nominal[:,:,index])
    if error is not None:
        views['error']=image(error_image(error,p))
    arrays={'nominal':nominal,'plates':plates,'alpha':alpha,'error':error}
    return views,arrays


def statistics(arrays,p):
    nominal=arrays['nominal']
    plates=arrays['plates']
    channels=ink_channels(p)
    w,h=pixel_size(p)
    report={'size':[w,h],'millimeters':[p['width_mm'],p['height_mm']],
            'dpi':p['dpi'],'lpi':p['lpi'],'pixels_per_cell':p['dpi']/p['lpi'],
            'separation':p['separation'],'inks':channels,
            'total_ink_max':float(nominal.sum(axis=2).max()),
            'total_ink_mean':float(nominal.sum(axis=2).mean()),
            'channels':{}}
    for index,channel in enumerate(channels):
        tone=nominal[:,:,index]
        plate=plates[:,:,index]
        report['channels'][channel['key']]={'name':channel['name'],'color':channel['color'],
                                 'mean_tone':float(tone.mean()),'area_coverage':float(plate.mean()),
                                 'min':float(tone.min()),'max':float(tone.max()),
                                 'histogram':np.histogram(tone,bins=16,range=(0,1))[0].tolist()}
    if arrays['error'] is not None:
        error=arrays['error']
        reachable=error<=ERROR_TOLERANCE
        report['reconstruction']={
            'metric':'线性反射率RMSE（分色请求值与调墨可表现值，0至1）',
            'tolerance':ERROR_TOLERANCE,'scale':p['error_scale'],
            'mean':float(error.mean()),'p95':float(np.percentile(error,95)),
            'max':float(error.max()),
            'within_tolerance':float(reachable.mean()),
            'unreachable_fraction':float(1-reachable.mean())}
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
    result={'rect':[l,t,r,b],'pixel_rect':[x0,y0,x1,y1],
            'nominal_mean':nominal.mean(axis=(0,1)).tolist(),
            'coverage':plates.mean(axis=(0,1)).tolist(),
            'pixel_count':int(nominal.shape[0]*nominal.shape[1])}
    if arrays['error'] is not None:
        block=arrays['error'][y0:y1,x0:x1]
        result['error_mean']=float(block.mean())
        result['error_max']=float(block.max())
    return result
