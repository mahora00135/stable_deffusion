import os
import sys

import uvicorn
from fastapi import FastAPI
from starlette.responses import FileResponse
from starlette.responses import StreamingResponse

app = FastAPI()

import argparse, os, sys, glob
import cv2
import torch
import numpy as np
from omegaconf import OmegaConf
from PIL import Image
from tqdm import tqdm, trange
from imwatermark import WatermarkEncoder
from itertools import islice
from einops import rearrange
from torchvision.utils import make_grid
import time
from pytorch_lightning import seed_everything
from torch import autocast
from contextlib import contextmanager, nullcontext

from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler

from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
from transformers import AutoFeatureExtractor

# load safety model
safety_model_id = "CompVis/stable-diffusion-safety-checker"
safety_feature_extractor = AutoFeatureExtractor.from_pretrained(safety_model_id)
safety_checker = StableDiffusionSafetyChecker.from_pretrained(safety_model_id)


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

    # model.cuda()
    model.eval()
    return model


def put_watermark(img, wm_encoder=None):
    if wm_encoder is not None:
        img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        img = wm_encoder.encode(img, 'dwtDct')
        img = Image.fromarray(img[:, :, ::-1])
    return img


def load_replacement(x):
    try:
        hwc = x.shape
        y = Image.open("assets/rick.jpeg").convert("RGB").resize((hwc[1], hwc[0]))
        y = (np.array(y) / 255.0).astype(x.dtype)
        assert y.shape == x.shape
        return y
    except Exception:
        return x


def check_safety(x_image):
    safety_checker_input = safety_feature_extractor(numpy_to_pil(x_image), return_tensors="pt")
    x_checked_image, has_nsfw_concept = safety_checker(images=x_image, clip_input=safety_checker_input.pixel_values)
    assert x_checked_image.shape[0] == len(has_nsfw_concept)
    for i in range(len(has_nsfw_concept)):
        if has_nsfw_concept[i]:
            x_checked_image[i] = load_replacement(x_checked_image[i])
    return x_checked_image, has_nsfw_concept


def txt2img(prompt_text: str, outpath, sampler):
    opt = {
        "prompt": prompt_text, "skip_grid": True,
        "skip_save": False, "ddim_steps": 50, "plms": False, "laion400m": False,
        "fixed_code": False, "ddim_eta": 0.0, "n_iter": 2, "H": 512,
        "W": 512, "C": 4, "f": 8, "n_samples": 1, "n_rows": 0, "scale": 7.5, "from_file": None,
        "precision": 'autocast'
    }

    print("Creating invisible watermark encoder (see https://github.com/ShieldMnt/invisible-watermark)...")
    wm = "StableDiffusionV1"
    wm_encoder = WatermarkEncoder()
    wm_encoder.set_watermark('bytes', wm.encode('utf-8'))

    batch_size = opt['n_samples']
    n_rows = opt['n_rows'] if opt['n_rows'] > 0 else batch_size
    if not opt['from_file']:
        prompt = opt['prompt']
        assert prompt is not None
        data = [batch_size * [prompt]]

    else:
        print(f"reading prompts from {opt['from_file']}")
        with open(opt['from_file'], "r") as f:
            data = f.read().splitlines()
            data = list(chunk(data, batch_size))

    sample_path = os.path.join(outpath, "samples")
    os.makedirs(sample_path, exist_ok=True)
    base_count = len(os.listdir(sample_path))
    grid_count = len(os.listdir(outpath)) - 1

    start_code = None
    if opt['fixed_code']:
        start_code = torch.randn([opt['n_samples'], opt['C'], opt['H'] // opt['f'], opt['W'] // opt['f']],
                                 device=device)

    precision_scope = autocast if opt['precision'] == "autocast" else nullcontext
    with torch.no_grad():
        with precision_scope("cuda"):
            with model.ema_scope():
                tic = time.time()
                all_samples = list()
                for n in trange(opt['n_iter'], desc="Sampling"):
                    for prompts in tqdm(data, desc="data"):
                        uc = None
                        if opt['scale'] != 1.0:
                            uc = model.get_learned_conditioning(batch_size * [""])
                        if isinstance(prompts, tuple):
                            prompts = list(prompts)
                        c = model.get_learned_conditioning(prompts)
                        shape = [opt['C'], opt['H'] // opt['f'], opt['W'] // opt['f']]
                        samples_ddim, _ = sampler.sample(S=opt['ddim_steps'],
                                                         conditioning=c,
                                                         batch_size=opt['n_samples'],
                                                         shape=shape,
                                                         verbose=False,
                                                         unconditional_guidance_scale=opt['scale'],
                                                         unconditional_conditioning=uc,
                                                         eta=opt['ddim_eta'],
                                                         x_T=start_code)

                        x_samples_ddim = model.decode_first_stage(samples_ddim)
                        x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
                        x_samples_ddim = x_samples_ddim.cpu().permute(0, 2, 3, 1).numpy()

                        x_checked_image, has_nsfw_concept = check_safety(x_samples_ddim)

                        x_checked_image_torch = torch.from_numpy(x_checked_image).permute(0, 3, 1, 2)

                        if not opt['skip_save']:
                            for x_sample in x_checked_image_torch:
                                x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                                img = Image.fromarray(x_sample.astype(np.uint8))
                                img = put_watermark(img, wm_encoder)
                                img.save(os.path.join(sample_path, f"{base_count:05}.png"))
                                img.save(f"./outputs/tmp.png")
                                base_count += 1

                        if not opt['skip_grid']:
                            all_samples.append(x_checked_image_torch)

                if not opt['skip_grid']:
                    # additionally, save as grid
                    grid = torch.stack(all_samples, 0)
                    grid = rearrange(grid, 'n b c h w -> (n b) c h w')
                    grid = make_grid(grid, nrow=n_rows)

                    # to image
                    grid = 255. * rearrange(grid, 'c h w -> h w c').cpu().numpy()
                    img = Image.fromarray(grid.astype(np.uint8))
                    img = put_watermark(img, wm_encoder)
                    img.save(os.path.join(outpath, f'grid-{grid_count:04}.png'))
                    img.save(f"./outputs/tmp.png")
                    grid_count += 1

                toc = time.time()

    print(f"Your samples are ready and waiting for you here: \n{outpath} \n"
          f" \nEnjoy.")


@app.post("/txt2img")
async def txt2img(prompt_text: str):
    txt2img(prompt_text, outdir, sampler)
    try:
        img = Image.open(f"./outputs/tmp.png")
        return StreamingResponse(img, media_type="image/png")
    except:
        return {"error": "output image not found"}


@app.get("/")
async def read_index():
    return FileResponse(f'{root_path}/api/index.html')


if __name__ == "__main__":
    print('strting operation')
    config = 'configs/stable-diffusion/v1-inference.yaml'
    ckpt = './sd-v1-4-full-ema.ckpt'
    outdir = 'outputs/txt2img-samples'
    seed = 42,
    seed_everything(seed)

    config = OmegaConf.load(config)
    model = load_model_from_config(config, ckpt)

    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    model = model.to(device)
    sampler = PLMSSampler(model)
    os.makedirs(outdir, exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=5555)
