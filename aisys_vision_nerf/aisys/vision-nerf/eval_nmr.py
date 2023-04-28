import os
import glob
import shutil
import configargparse
import tqdm
import imageio
import numpy as np
import torch
from torch.utils.data import Dataset
import torch.nn.functional as F
import torchvision.transforms as T

from models.render_image import render_single_image
from models.model import VisionNerfModel
from models.sample_ray import RaySamplerSingleImage
from models.projection import Projector
from utils import img_HWC2CHW

def config_parser():
    parser = configargparse.ArgumentParser()
    # general
    parser.add_argument('--config', is_config_file=True, help='config file path')
    parser.add_argument('--expname', type=str, help='experiment name')
    parser.add_argument('--ckptdir', type=str, help='checkpoint folder')
    parser.add_argument('--ckpt_path', type=str, default='',
                        help='specific weights npy file to reload for coarse network')
    parser.add_argument('--outdir', type=str, help='output video directory')
    parser.add_argument("--local_rank", type=int, default=0, help='rank for distributed training')
    parser.add_argument("--include_src", action="store_true", help="Include source views in calculation")

    ########## dataset options ##########
    ## render dataset
    parser.add_argument('--data_path', type=str, help='the dataset to train')
    parser.add_argument('--data_type', type=str, default='srn', help='dataset type to use')
    parser.add_argument('--img_hw', type=int, nargs='+', help='image size option for dataset')
    parser.add_argument('--data_range', type=int,
                        default=[0, 50],
                        nargs='+',
                        help='data index to select from the dataset')
    parser.add_argument('--data_indices', type=int,
                        default=[0],
                        nargs='+',
                        help='data index to select from the dataset')
    parser.add_argument('--use_data_index', action='store_true',
                        help='use data_indices instead of data_range')
    parser.add_argument('--pose_index', type=int,
                        default=64,
                        help='source pose index to select from the dataset')
    parser.add_argument('--source_view_list', type=str, default="",
                        help='path to source view list, overrides pose_index if not empty')
    parser.add_argument('--no_reload', action='store_true',
                        help='do not reload weights from saved ckpt (not used)')
    parser.add_argument('--distributed', action='store_true', help='if use distributed training (not used)')
    parser.add_argument('--skip', type=int,
                        default=1,
                        help='camera pose skip')
    parser.add_argument("--multicat", action="store_true",
                        help="Prepend category id to object id. Specify if model fits multiple categories.")

    ########## model options ##########
    ## ray sampling options
    parser.add_argument('--chunk_size', type=int, default=128,
                        help='number of rays processed in parallel, decrease if running out of memory')
    
    ## model options
    parser.add_argument('--im_feat_dim', type=int, default=128, help='image feature dimension')
    parser.add_argument('--mlp_feat_dim', type=int, default=512, help='mlp hidden dimension')
    parser.add_argument('--freq_num', type=int, default=10, help='how many frequency bases for positional encodings')
    parser.add_argument('--mlp_block_num', type=int, default=2, help='how many resnet blocks for coarse network')
    parser.add_argument('--coarse_only', action='store_true', help='use coarse network only')
    parser.add_argument("--anti_alias_pooling", type=int, default=1, help='if use anti-alias pooling')
    parser.add_argument('--num_source_views', type=int, default=1, help='number of views')
    parser.add_argument('--freeze_pos_embed', action='store_true', help='freeze positional embeddings')
    parser.add_argument('--no_skip_conv', action='store_true', help='disable skip convolution')

    ########### iterations & learning rate options (not used) ##########
    parser.add_argument('--lrate_feature', type=float, default=1e-3, help='learning rate for feature extractor')
    parser.add_argument('--lrate_mlp', type=float, default=5e-4, help='learning rate for mlp')
    parser.add_argument('--lrate_decay_factor', type=float, default=0.5,
                        help='decay learning rate by a factor every specified number of steps')
    parser.add_argument('--lrate_decay_steps', type=int, default=50000,
                        help='decay learning rate by a factor every specified number of steps')
    parser.add_argument('--warmup_steps', type=int, default=10000, help='num of iterations for warm-up')
    parser.add_argument('--scheduler', type=str, default='steplr', help='scheduler type to use [steplr]')
    parser.add_argument('--use_warmup', action='store_true', help='use warm-up scheduler')
    parser.add_argument('--bbox_steps', type=int, default=100000, help='iterations to use bbox sampling')

    ########## rendering options ##########
    parser.add_argument('--N_samples', type=int, default=64, help='number of coarse samples per ray')
    parser.add_argument('--N_importance', type=int, default=64, help='number of important samples per ray')
    parser.add_argument('--inv_uniform', action='store_true',
                        help='if True, will uniformly sample inverse depths')
    parser.add_argument('--det', action='store_true', help='deterministic sampling for coarse and fine samples')
    parser.add_argument('--white_bkgd', action='store_true',
                        help='apply the trick to avoid fitting to white background')

    return parser

def parse_pose_dvr(path, num_views):
    cameras = np.load(path)

    intrinsics = []
    c2w_mats = []

    for i in range(num_views):
        # ShapeNet
        wmat_inv_key = "world_mat_inv_" + str(i)
        wmat_key = "world_mat_" + str(i)
        kmat_key = "camera_mat_" + str(i)
        if wmat_inv_key in cameras:
            c2w_mat = cameras[wmat_inv_key]
        else:
            w2c_mat = cameras[wmat_key]
            if w2c_mat.shape[0] == 3:
                w2c_mat = np.vstack((w2c_mat, np.array([0, 0, 0, 1])))
            c2w_mat = np.linalg.inv(w2c_mat)

        intrinsics.append(cameras[kmat_key])
        c2w_mats.append(c2w_mat)

    intrinsics = np.stack(intrinsics, 0)
    c2w_mats = np.stack(c2w_mats, 0)

    return intrinsics, c2w_mats

class DVRRenderDataset(Dataset):
    """
    Dataset for rendering
    """
    def __init__(self, args, mode="test", **kwargs):
        """
        Args:
            args.data_path: path to data directory
            args.img_hw: image size (resize if needed)
        """
        super().__init__()
        self.base_path = args.data_path
        self.dataset_name = os.path.basename(args.data_path)
        assert os.path.exists(self.base_path)

        cats = [x for x in glob.glob(os.path.join(args.data_path, "*")) if os.path.isdir(x)]

        list_prefix = "softras_"

        if mode == "train":
            file_lists = [os.path.join(x, list_prefix + "train.lst") for x in cats]
        elif mode == "val":
            file_lists = [os.path.join(x, list_prefix + "val.lst") for x in cats]
        elif mode == "test":
            file_lists = [os.path.join(x, list_prefix + "test.lst") for x in cats]

        print("Loading NMR dataset", self.base_path, "name:", self.dataset_name, "mode:", mode)

        self.mode = mode

        all_objs = []
        for file_list in file_lists:
            if not os.path.exists(file_list):
                continue
            base_dir = os.path.dirname(file_list)
            cat = os.path.basename(base_dir)
            with open(file_list, "r") as f:
                objs = [(cat, os.path.join(base_dir, x.strip())) for x in f.readlines()]
            all_objs.extend(objs)

        self.all_objs = all_objs

        # self.all_objs = self.all_objs[:100] # HACK to skip all other dataset

        self.intrinsics = []
        self.poses = []
        self.rgb_paths = []
        for _, path in tqdm.tqdm(self.all_objs):
            curr_paths = sorted(glob.glob(os.path.join(path, "image", "*")))
            self.rgb_paths.append(curr_paths)

            pose_path = os.path.join(path, 'cameras.npz')
            intrinsics, c2w_mats = parse_pose_dvr(pose_path, len(curr_paths))

            self.poses.append(c2w_mats)
            self.intrinsics.append(intrinsics)

        self.rgb_paths = np.array(self.rgb_paths)
        self.poses = np.stack(self.poses, 0)
        self.intrinsics = np.array(self.intrinsics)

        assert(len(self.rgb_paths) == len(self.poses))

        self.define_transforms()
        self.img_hw = args.img_hw

        # default near/far plane depth
        self.z_near = 1.2
        self.z_far = 4.0

    def __len__(self):
        return len(self.intrinsics)

    def define_transforms(self):
        self.img_transforms = T.Compose(
            [T.ToTensor(), T.Normalize((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))]
        )
        self.mask_transforms = T.Compose(
            [T.ToTensor(), T.Normalize((0.0,), (1.0,))]
        )

    def __getitem__(self, index):
        # Read source RGB
        src_rgb_paths = self.rgb_paths[index]
        src_c2w_mats = self.poses[index]
        src_intrinsics = self.intrinsics[index].copy()

        src_rgbs = []
        src_masks = []
        for i, rgb_path in enumerate(src_rgb_paths):
            img = imageio.imread(rgb_path)[..., :3]
            mask = (img.sum(axis=-1) != 255*3)[..., None].astype(np.uint8) * 255
            rgb = self.img_transforms(img)
            mask = self.mask_transforms(mask)

            src_intrinsics[i, 0, 0] *= img.shape[1] / 2.0
            src_intrinsics[i, 1, 1] *= img.shape[0] / 2.0
            src_intrinsics[i, 0, 2] = img.shape[1] / 2.0
            src_intrinsics[i, 1, 2] = img.shape[0] / 2.0

            h, w = rgb.shape[-2:]
            if (h != self.img_hw[0]) or (w != self.img_hw[1]):
                scale = self.img_hw[-1] / w
                src_intrinsics[i, :2] *= scale

                rgb = F.interpolate(rgb[None, :], size=self.img_hw, mode="area")[0]
                mask = F.interpolate(mask[None, :], size=self.img_hw, mode="area")[0]
            
            src_rgbs.append(rgb)
            src_masks.append(mask)

        depth_range = np.array([self.z_near, self.z_far])

        return {
            "rgb_path": rgb_path,
            "img_id": index,
            "img_hw": self.img_hw,
            "src_masks": torch.stack(src_masks).permute([0, 2, 3, 1]).float(),
            "src_rgbs": torch.stack(src_rgbs).permute([0, 2, 3, 1]).float(),
            "src_c2w_mats": torch.FloatTensor(src_c2w_mats),
            "src_intrinsics": torch.FloatTensor(src_intrinsics),
            "depth_range": torch.FloatTensor(depth_range)
        }

def gen_eval(args):

    device = "cuda"
    print(f"checkpoints reload from {args.ckptdir}")

    dataset = DVRRenderDataset(args)

    # Create VisionNeRF model
    model = VisionNerfModel(args, False, False)
    # create projector
    projector = Projector(device=device)
    model.switch_to_eval()

    if args.use_data_index:
        data_index = args.data_indices
    else:
        data_index = np.arange(args.data_range[0], args.data_range[1])

    use_source_list = len(args.source_view_list) > 0
    if use_source_list:
        print("Using views from list", args.source_view_list)
        with open(args.source_view_list, "r") as f:
            tmp = [x.strip().split() for x in f.readlines()]
        pose_indices = {
            x[0] + "/" + x[1]: torch.tensor(list(map(int, x[2:])), dtype=torch.long)
            for x in tmp
        }
    else:
        pose_indices = [args.pose_index]

    for d_idx in data_index:      
        
        dpath = dataset[d_idx]["rgb_path"][:-15]

        obj_basename = os.path.basename(dpath)
        cat_name = os.path.basename(os.path.dirname(dpath))
        obj_name = cat_name + "_" + obj_basename if args.multicat else obj_basename

        out_folder = os.path.join(args.outdir, args.expname, obj_name)
        print(f'Rendering {dpath}')
        print(f'images will be saved to {out_folder}')
        os.makedirs(out_folder, exist_ok=True)

        # save the args and config files
        f = os.path.join(out_folder, 'args.txt')
        with open(f, 'w') as file:
            for arg in sorted(vars(args)):
                attr = getattr(args, arg)
                file.write('{} = {}\n'.format(arg, attr))

        if args.config is not None:
            f = os.path.join(out_folder, 'config.txt')
            if not os.path.isfile(f):
                shutil.copy(args.config, f)

        sample = dataset[d_idx]

        if use_source_list:
            obj_id = cat_name + '/' + obj_basename
            pose_index = pose_indices[obj_id][0]
        else:
            pose_index = pose_indices[0]

        data_input = dict(
            rgb_path=sample['rgb_path'],
            img_id=sample['img_id'],
            img_hw=sample['img_hw'],
            tgt_intrinsic=sample['src_intrinsics'][0:1],
            src_masks=sample['src_masks'][pose_index][None, None, :],
            src_rgbs=sample['src_rgbs'][pose_index][None, None, :],
            src_c2w_mats=sample['src_c2w_mats'][pose_index][None, None, :],
            src_intrinsics=sample['src_intrinsics'][pose_index][None, None, :],
            depth_range=sample['depth_range'][None, :]
        )

        input_im = sample['src_rgbs'][pose_index].cpu().numpy() * 255.
        input_im = input_im.astype(np.uint8)
        filename = os.path.join(out_folder, 'input.png')

        imageio.imwrite(filename, input_im)

        render_poses = sample['src_c2w_mats']
        view_indices = np.arange(0, len(render_poses), args.skip)
        render_poses = render_poses[view_indices]

        imgs = []
        with torch.no_grad():

            for idx, pose in tqdm.tqdm(zip(view_indices, render_poses), total=len(view_indices)):
                if not args.include_src and idx == pose_index:
                    continue
                filename = os.path.join(out_folder, f'{idx:06}.png')
                data_input['tgt_c2w_mat'] = pose[None, :]

                # load training rays
                ray_sampler = RaySamplerSingleImage(data_input, device, render_stride=1)
                ray_batch = ray_sampler.get_all()
                featmaps = model.encode(ray_batch['src_rgbs'])

                ret = render_single_image(ray_sampler=ray_sampler,
                                          ray_batch=ray_batch,
                                          model=model,
                                          projector=projector,
                                          chunk_size=args.chunk_size,
                                          N_samples=args.N_samples,
                                          inv_uniform=args.inv_uniform,
                                          N_importance=args.N_importance,
                                          det=True,
                                          white_bkgd=args.white_bkgd,
                                          render_stride=1,
                                          featmaps=featmaps)
                
                rgb_im = img_HWC2CHW(ret['outputs_fine']['rgb'].detach().cpu())
                # clamping RGB images
                rgb_im = torch.clamp(rgb_im, 0.0, 1.0)
                rgb_im = rgb_im.permute([1, 2, 0]).cpu().numpy()

                rgb_im = (rgb_im * 255.).astype(np.uint8)
                imageio.imwrite(filename, rgb_im)
                imgs.append(rgb_im)
                torch.cuda.empty_cache()

if __name__ == '__main__':
    parser = config_parser()
    args = parser.parse_args()

    gen_eval(args)
