import os
os.environ["XDG_CACHE_HOME"] = "../../.cache"

import sys

sys.stdout.write("Imports ...\n")
sys.stdout.flush()

import argparse, os, shutil
from typing import List, Optional
import cv2
import torch
import cv2
import PIL
import gc
import numpy as np
import subprocess
from omegaconf import OmegaConf
from PIL import Image, ImageDraw
from tqdm import tqdm, trange
#from imwatermark import WatermarkEncoder
from einops import rearrange, repeat
from itertools import islice
from einops import rearrange
import time
from pytorch_lightning import seed_everything
from torch.cuda.amp import autocast

from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddimHD import DDIMSampler
from ldm.models.diffusion.plmsHD import PLMSSampler

from contextlib import contextmanager, nullcontext

"""
def put_watermark(img, wm_encoder=None):
    if wm_encoder is not None:
        img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        img = wm_encoder.encode(img, 'dwtDct')
        img = Image.fromarray(img[:, :, ::-1])
    return img
"""

def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())

def numpy_to_pil(images):
    """
    Convert a numpy image or a batch of images to a PIL image.
    """
    if images.ndim == 3:
        images = images[None, ...]
    images = (images * 255).round().astype("uint8")
    pil_images = [Image.fromarray(image) for image in images]

    return pil_images

def load_model_from_config(config, ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)

    model.cuda()
    model.eval()
    return model

def load_img(path):
    image = Image.open(path).convert("RGB")
    w, h = image.size
    print(f"loaded input image of size ({w}, {h}) from {path}")
    w, h = map(lambda x: x - x % 32, (w, h))  # resize to integer multiple of 32
    image = image.resize((w, h), resample=PIL.Image.Resampling.LANCZOS)
    image = np.array(image).astype(np.float32) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    return 2.*image - 1.

def convert_pil_img(image):
    w, h = image.size
    w, h = map(lambda x: x - x % 32, (w, h))  # resize to integer multiple of 32
    image = image.resize((w, h), resample=PIL.Image.Resampling.LANCZOS)
    image = np.array(image).astype(np.float32) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    return 2.*image - 1.

def addalpha(im, mask):
    imr, img, imb, ima = im.split()
    mmr, mmg, mmb, mma = mask.split()
    im = Image.merge('RGBA', [imr, img, imb, mma])  # we want the RGB from the original, but the transparency from the mask
    return(im)

# Alternative method composites a grid of images at the positions provided
def grid_merge(source, slices):
    source.convert("RGBA")
    for slice, posx, posy in slices: # go in reverse to get proper stacking
        source.alpha_composite(slice, (posx, posy))
    return source

def grid_coords(target, original, overlap):
    #generate a list of coordinate tuples for our sections, in order of how they'll be rendered
    #target should be the size for the gobig result, original is the size of each chunk being rendered
    center = []
    target_x, target_y = target
    center_x = int(target_x / 2)
    center_y = int(target_y / 2)
    original_x, original_y = original
    x = center_x - int(original_x / 2)
    y = center_y - int(original_y / 2)
    center.append((x,y)) #center chunk
    uy = y #up
    uy_list = []
    dy = y #down
    dy_list = []
    lx = x #left
    lx_list = []
    rx = x #right
    rx_list = []
    while uy > 0: #center row vertical up
        uy = uy - original_y + overlap
        uy_list.append((lx, uy))
    while (dy + original_y) <= target_y: #center row vertical down
        dy = dy + original_y - overlap
        dy_list.append((rx, dy))
    while lx > 0:
        lx = lx - original_x + overlap
        lx_list.append((lx, y))
        uy = y
        while uy > 0:
            uy = uy - original_y + overlap
            uy_list.append((lx, uy))
        dy = y
        while (dy + original_y) <= target_y:
            dy = dy + original_y - overlap
            dy_list.append((lx, dy))
    while (rx + original_x) <= target_x:
        rx = rx + original_x - overlap
        rx_list.append((rx, y))
        uy = y
        while uy > 0:
            uy = uy - original_y + overlap
            uy_list.append((rx, uy))
        dy = y
        while (dy + original_y) <= target_y:
            dy = dy + original_y - overlap
            dy_list.append((rx, dy))
    # calculate a new size that will fill the canvas, which will be optionally used in grid_slice and go_big
    last_coordx, last_coordy = dy_list[-1:][0]
    render_edgey = last_coordy + original_y # outer bottom edge of the render canvas
    render_edgex = last_coordx + original_x # outer side edge of the render canvas
    scalarx = render_edgex / target_x
    scalary = render_edgey / target_y
    if scalarx <= scalary:
        new_edgex = int(target_x * scalarx)
        new_edgey = int(target_y * scalarx)
    else:
        new_edgex = int(target_x * scalary)
        new_edgey = int(target_y * scalary)
    # now put all the chunks into one master list of coordinates (essentially reverse of how we calculated them so that the central slices will be on top)
    result = []
    for coords in dy_list[::-1]:
        result.append(coords)
    for coords in uy_list[::-1]:
        result.append(coords)
    for coords in rx_list[::-1]:
        result.append(coords)
    for coords in lx_list[::-1]:
        result.append(coords)
    result.append(center[0])
    return result, (new_edgex, new_edgey)

def get_resampling_mode():
    try:
        from PIL import __version__, Image
        major_ver = int(__version__.split('.')[0])
        if major_ver >= 9:
            return Image.Resampling.LANCZOS
        else:
            return Image.LANCZOS
    except Exception as ex:
        return 1  # 'Lanczos' irrespective of version.

# Chop our source into a grid of images that each equal the size of the original render
def grid_slice(source, overlap, og_size, maximize=False): 
    width, height = og_size # size of the slices to be rendered
    coordinates, new_size = grid_coords(source.size, og_size, overlap)
    if maximize == True:
        source = source.resize(new_size, get_resampling_mode()) # minor concern that we're resizing twice
        coordinates, new_size = grid_coords(source.size, og_size, overlap) # re-do the coordinates with the new canvas size
    # loc_width and loc_height are the center point of the goal size, and we'll start there and work our way out
    slices = []
    for coordinate in coordinates:
        x, y = coordinate
        slices.append(((source.crop((x, y, x+width, y+height))), x, y))
    global slices_todo
    slices_todo = len(slices) - 1
    return slices, new_size

class Options:
    prompt: List[str]
    outdir: str
    steps: int
    n_iter: int
    H: int
    W: int
    C: int
    f: int
    scale: float
    strength: float
    from_file: bool
    config: str
    ckpt: str
    passes: int
    wm: str
    realesrgan: str
    detail_steps: int
    detail_scale: float
    gobig_overlap: int
    generated: Optional[List[str]]
    img: str

def main():
    
    sys.stdout.write("Parsing arguments ...\n")
    sys.stdout.flush()

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--prompt",
        type=str,
        nargs="?",
        help="the prompt to render"
    )
    parser.add_argument(
        "--generated",
        type=str,
        nargs="?",
        help="only do detailing, using these base filenames in output dir"
    )
    parser.add_argument(
        "--img",
        type=str,
        nargs="?",
        help="only do detailing, using this path (will be copied to output dir)"
    )
    parser.add_argument(
        "--outdir",
        type=str,
        nargs="?",
        help="dir to write results to",
        default="outputs/txt2imghd-samples"
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=150,
        help="number of sampling steps",
    )
    parser.add_argument(
        "--ddim",
        action='store_true',
        help="use ddim sampling",
    )
    parser.add_argument(
        "--n_iter",
        type=int,
        default=1,
        help="sample this often",
    )
    parser.add_argument(
        "--H",
        type=int,
        default=1152,
        help="image height, in pixel space",
    )
    parser.add_argument(
        "--W",
        type=int,
        default=512,
        help="image width, in pixel space",
    )
    parser.add_argument(
        "--C",
        type=int,
        default=4,
        help="latent channels",
    )
    parser.add_argument(
        "--f",
        type=int,
        default=8,
        help="downsampling factor",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=10,
        help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
    )
    parser.add_argument(
        "--from-file",
        type=str,
        help="if specified, load prompts from this file",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="the seed (for reproducible sampling)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="configs/stable-diffusion/v1-inference.yaml",
        help="path to config which constructs model",
    )
    parser.add_argument(
        "--ckpt",
        type=str,
        default="models/ldm/stable-diffusion-v1/sd-v1-4.ckpt",
        help="path to checkpoint of model",
    )
    parser.add_argument(
        "--strength",
        type=float,
        default=0.3,
        help="strength for noising/unnoising. 1.0 corresponds to full destruction of information in init image",
    )
    parser.add_argument(
        "--passes",
        type=int,
        default=1,
        help="number of upscales/details",
    )
    """
    parser.add_argument(
        "--wm",
        type=str,
        default="txt2imghd",
        help="watermark text",
    )
    """
    parser.add_argument(
        "--realesrgan",
        type=str,
        default="realesrgan-ncnn-vulkan",
        help="path to realesrgan executable"
    )
    parser.add_argument(
        "--detail_steps",
        type=int,
        default=150,
        help="number of sampling steps when detailing",
    )
    parser.add_argument(
        "--detail_scale",
        type=float,
        default=10,
        help="unconditional guidance scale when detailing: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
    )
    parser.add_argument(
        "--gobig_overlap",
        type=int,
        default=128,
        help="overlap size for GOBIG",
    )

    parser.add_argument("--embedding_type", type=str, help=".bin or .pt")
    parser.add_argument("--embedding_path", type=str, help="Path to a pre-trained embedding manager checkpoint")
    parser.add_argument("--negative_prompt", type=str, help="negative prompt")
    parser.add_argument('--seamless',action='store_true',default=False,help='Change the model to seamless tiling (circular) mode',)

    parser.add_argument("--image_file", type=str)

    opt = parser.parse_args()

    if opt.prompt is None:
        opt.prompt = input("prompt: ")
        
        
       
    text2img2(opt)

def realesrgan2x(executable: str, input: str, output: str):
    print(f"executable = {executable}\n")
    print(f"input = {input}\n")
    print(f"output = {output}\n")
    process = subprocess.Popen([
        './'+executable+'/realesrgan-ncnn-vulkan.exe',
        '-i',
        './'+input,
        '-o',
        './'+output,
        '-n',
        'realesrgan-x4plus'
    ])
    process.wait()

    final_output = Image.open(output)
    final_output = final_output.resize((int(final_output.size[0] / 2), int(final_output.size[1] / 2)), get_resampling_mode())
    final_output.save(output)

def text2img2(opt: Options):

    seed_everything(opt.seed)

    sys.stdout.write("Loading model ...\n")
    sys.stdout.flush()

    #seamless
    def patch_conv(cls):
        init = cls.__init__
        def __init__(self, *args, **kwargs):
                return init(self, *args, **kwargs, padding_mode='circular')
        cls.__init__ = __init__
    
    if opt.seamless:
        print(">> changed to seamless tiling mode")
        patch_conv(torch.nn.Conv2d)
        
    import shutil

    originalpt = 'ldm/modules/embedding_managerpt.py'

    originalbin = 'ldm/modules/embedding_managerbin.py'

    if opt.embedding_type == ".pt":
        file_path = "ldm/modules/embedding_manager.py"
        if os.path.isfile(file_path):
            os.remove(file_path)
            shutil.copyfile(originalpt, file_path)
            print('using .pt embedding')
    elif opt.embedding_type == ".bin":
        file_path = "ldm/modules/embedding_manager.py"
        if os.path.isfile(file_path):
            os.remove(file_path)
            shutil.copyfile(originalbin, file_path)
            print('using .bin embedding')  
            
    config = OmegaConf.load(f"{opt.config}")
    model = load_model_from_config(config, f"{opt.ckpt}")
    if opt.embedding_path is not None:
        model.embedding_manager.load(opt.embedding_path)

    #fix for using less VRAM 1/3 - add next line
    #model.half()

    device = torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")
    print('Using device:', device)
    print(torch.cuda.get_device_properties(device))
    sys.stdout.flush()
    model = model.to(device)

    if opt.ddim:
        sampler = DDIMSampler(model)
    else:
        sampler = PLMSSampler(model)

    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir

    sample_path = os.path.join(outpath, "samples")
    os.makedirs(sample_path, exist_ok=True)

    #wm_encoder = WatermarkEncoder()
    #wm_encoder.set_watermark('bytes', opt.wm.encode('utf-8'))

    batch_size = 1
    
    #precision_scope = autocast
    precision_scope = nullcontext
    
    base_count = len(os.listdir(sample_path))

    if not opt.from_file:
        prompt = opt.prompt
        assert prompt is not None
        data = [batch_size * [prompt]]

    else:
        print(f"reading prompts from {opt.from_file}")
        with open(opt.from_file, "r") as f:
            data = f.read().splitlines()
            data = list(chunk(data, batch_size))

    generated = opt.generated
    if generated is None and opt.img is not None:
        shutil.copyfile(opt.img, os.path.join(sample_path, f"{base_count:05}.png"))
        #shutil.copyfile(opt.img, opt.image_file)
        
        sys.stdout.write('Saving progress ...\n')
        sys.stdout.flush()
        shutil.copyfile(os.path.join(sample_path, f"{base_count:05}.png"),opt.image_file)
        sys.stdout.write('Progress saved\n')
        sys.stdout.flush()
        
        generated = [f"{base_count:05}"]
    elif isinstance(generated, str):
        generated = [generated]
    
    if generated is None:
        generated = []
        with torch.inference_mode():
            with precision_scope("cuda"):
                with model.ema_scope():
                    #for _ in trange(opt.n_iter, desc="Sampling"):
                    for _ in range(opt.n_iter):
                    
                        #sys.stdout.write(f'Iteration {_}\n')
                        #sys.stdout.flush()
                        
                        for prompts in tqdm(data, desc="data"):
                            uc = None
                            if opt.scale != 1.0:
                                uc = model.get_learned_conditioning(batch_size * [opt.negative_prompt])
                            if isinstance(prompts, tuple):
                                prompts = list(prompts)
                            c = model.get_learned_conditioning(prompts)
                            shape = [opt.C, opt.H // opt.f, opt.W // opt.f]
                            samples_ddim, _ = sampler.sample(S=opt.steps,
                                                            conditioning=c,
                                                            batch_size=batch_size,
                                                            shape=shape,
                                                            verbose=False,
                                                            unconditional_guidance_scale=opt.scale,
                                                            unconditional_conditioning=uc,
                                                            eta=0,
                                                            x_T=None)

                            x_samples_ddim = model.decode_first_stage(samples_ddim)
                            x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
                            x_samples_ddim = x_samples_ddim.cpu().permute(0, 2, 3, 1).numpy()

                            x_checked_image = x_samples_ddim

                            x_checked_image_torch = torch.from_numpy(x_checked_image).permute(0, 3, 1, 2)

                            for x_sample in x_checked_image_torch:
                                x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                                img = Image.fromarray(x_sample.astype(np.uint8))
                                output_path = os.path.join(sample_path, f"{base_count:05}.png")
                                #output_path = opt.image_file
                                img.save(output_path)

                                sys.stdout.write('Saving progress ...\n')
                                sys.stdout.flush()
                                shutil.copyfile(output_path,opt.image_file)
                                sys.stdout.write('Progress saved\n')
                                sys.stdout.flush()


                                generated.append(f"{base_count:05}")
                                base_count += 1

        torch.cuda.empty_cache()
        gc.collect()

    sampler = DDIMSampler(model)

    for base_filename in generated:
        for _ in trange(opt.passes, desc="Passes"):
            realesrgan2x(opt.realesrgan, os.path.join(sample_path, f"{base_filename}.png"), os.path.join(sample_path, f"{base_filename}u.png"))
            #realesrgan2x(opt.realesrgan, opt.image_file, opt.image_file)
            base_filename = f"{base_filename}u"

            source_image = Image.open(os.path.join(sample_path, f"{base_filename}.png"))
            
            sys.stdout.write('Saving progress ...\n')
            sys.stdout.flush()
            shutil.copyfile(os.path.join(sample_path, f"{base_filename}.png"),opt.image_file)
            sys.stdout.write('Progress saved\n')
            sys.stdout.flush()
            
            #source_image = Image.open(opt.image_file)
            og_size = (opt.H,opt.W)
            slices, _ = grid_slice(source_image, opt.gobig_overlap, og_size, False)

            betterslices = []
            for _, chunk_w_coords in tqdm(enumerate(slices), "Slices", disable=True):
            
                #sys.stdout.write(f'Slice {_}\n')
                #sys.stdout.flush()

                chunk, coord_x, coord_y = chunk_w_coords
                init_image = convert_pil_img(chunk).to(device)
                init_image = repeat(init_image, '1 ... -> b ...', b=batch_size)
                
                #fix for using less VRAM 2/3 next line added
                #with torch.cuda.amp.autocast(): # needed for half precision!
                init_latent = model.get_first_stage_encoding(model.encode_first_stage(init_image))  # move to latent space

                sampler.make_schedule(ddim_num_steps=opt.detail_steps, ddim_eta=0, verbose=False)

                assert 0. <= opt.strength <= 1., 'can only work with strength in [0.0, 1.0]'
                t_enc = int(opt.strength * opt.detail_steps)

                with torch.inference_mode():
        
                    #with precision_scope("cuda"):
                    #fix for using less VRAM 3/3 - change previous line to this
                    with torch.cuda.amp.autocast():
                    
                        with model.ema_scope():
                            for prompts in tqdm(data, desc="data"):

                                uc = None
                                if opt.detail_scale != 1.0:
                                    uc = model.get_learned_conditioning(batch_size * [opt.negative_prompt])
                                if isinstance(prompts, tuple):
                                    prompts = list(prompts)
                                c = model.get_learned_conditioning(prompts)

                                # encode (scaled latent)
                                z_enc = sampler.stochastic_encode(init_latent, torch.tensor([t_enc]*batch_size).to(device))
                                # decode it
                                samples = sampler.decode(z_enc, c, t_enc, unconditional_guidance_scale=opt.detail_scale,
                                                        unconditional_conditioning=uc,)

                                x_samples = model.decode_first_stage(samples)
                                x_samples = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)

                                for x_sample in x_samples:
                                    x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                                    resultslice = Image.fromarray(x_sample.astype(np.uint8)).convert('RGBA')
                                    betterslices.append((resultslice.copy(), coord_x, coord_y))

            alpha = Image.new('L', og_size, color=0xFF)
            alpha_gradient = ImageDraw.Draw(alpha)
            a = 0
            i = 0
            overlap = opt.gobig_overlap
            shape = (og_size, (0,0))
            while i < overlap:
                alpha_gradient.rectangle(shape, fill = a)
                a += 4
                i += 1
                shape = ((og_size[0] - i, og_size[1]- i), (i,i))
            mask = Image.new('RGBA', og_size, color=0)
            mask.putalpha(alpha)
            finished_slices = []
            for betterslice, x, y in betterslices:
                finished_slice = addalpha(betterslice, mask)
                finished_slices.append((finished_slice, x, y))
            # # Once we have all our images, use grid_merge back onto the source, then save
            final_output = grid_merge(source_image.convert("RGBA"), finished_slices).convert("RGB")
            final_output.save(os.path.join(sample_path, f"{base_filename}d.png"))

            sys.stdout.write('Saving progress ...\n')
            sys.stdout.flush()
            shutil.copyfile(os.path.join(sample_path, f"{base_filename}d.png"),opt.image_file)
            sys.stdout.write('Progress saved\n')
            sys.stdout.flush()

            #final_output.save(opt.image_file)
            base_filename = f"{base_filename}d"

            torch.cuda.empty_cache()
            gc.collect()
        
        #put_watermark(final_output, wm_encoder)
        #final_output.save(os.path.join(sample_path, f"{base_filename}.png"))

if __name__ == "__main__":
    main()