# Copyright Niantic 2019. Patent Pending. All rights reserved.
#
# This software is licensed under the terms of the Monodepth2 licence
# which allows for non-commercial use only, the full terms of which are made
# available in the LICENSE file.

from __future__ import absolute_import, division, print_function

import numpy as np
import time

import torch
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader
from tensorboardX import SummaryWriter

import json

from utils import *
from kitti_utils import *
from layers import *

import datasets
import networks
from IPython import embed


import sys
sys.path.append('/home/minghanz/pytorch-unet')
from geometry_plot import draw3DPts
from geometry import gramian, kern_mat, rgb_to_hsv

import torch
torch.manual_seed(0)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

def my_collate_fn(batch):
    batch_new = {}
    for item in batch[0]:
        batch_new[item] = {}
        if "velo_gt" not in item:
            batch_new[item] = torch.stack([batchi[item] for batchi in batch], 0)
        else:
            batch_new[item] = [batchi[item].unsqueeze(0) for batchi in batch]
    return batch_new

class Trainer:
    def __init__(self, options):
        self.opt = options
        self.log_path = os.path.join(self.opt.log_dir, self.opt.model_name)

        # checking height and width are multiples of 32
        assert self.opt.height % 32 == 0, "'height' must be a multiple of 32"
        assert self.opt.width % 32 == 0, "'width' must be a multiple of 32"

        self.models = {}
        self.parameters_to_train = []

        self.device = torch.device("cpu" if self.opt.no_cuda else "cuda:1")

        self.num_scales = len(self.opt.scales)
        self.num_input_frames = len(self.opt.frame_ids)
        self.num_pose_frames = 2 if self.opt.pose_model_input == "pairs" else self.num_input_frames

        assert self.opt.frame_ids[0] == 0, "frame_ids must start with 0"

        self.use_pose_net = not (self.opt.use_stereo and self.opt.frame_ids == [0])

        if self.opt.use_stereo:
            self.opt.frame_ids.append("s")

        self.models["encoder"] = networks.ResnetEncoder(
            self.opt.num_layers, self.opt.weights_init == "pretrained")
        self.models["encoder"].to(self.device)
        self.parameters_to_train += list(self.models["encoder"].parameters())

        self.models["depth"] = networks.DepthDecoder(
            self.models["encoder"].num_ch_enc, self.opt.scales)
        self.models["depth"].to(self.device)
        self.parameters_to_train += list(self.models["depth"].parameters())

        if self.use_pose_net:
            if self.opt.pose_model_type == "separate_resnet":
                self.models["pose_encoder"] = networks.ResnetEncoder(
                    self.opt.num_layers,
                    self.opt.weights_init == "pretrained",
                    num_input_images=self.num_pose_frames)

                self.models["pose_encoder"].to(self.device)
                self.parameters_to_train += list(self.models["pose_encoder"].parameters())

                self.models["pose"] = networks.PoseDecoder(
                    self.models["pose_encoder"].num_ch_enc,
                    num_input_features=1,
                    num_frames_to_predict_for=2)

            elif self.opt.pose_model_type == "shared":
                self.models["pose"] = networks.PoseDecoder(
                    self.models["encoder"].num_ch_enc, self.num_pose_frames)

            elif self.opt.pose_model_type == "posecnn":
                self.models["pose"] = networks.PoseCNN(
                    self.num_input_frames if self.opt.pose_model_input == "all" else 2)

            self.models["pose"].to(self.device)
            self.parameters_to_train += list(self.models["pose"].parameters())

        if self.opt.predictive_mask:
            assert self.opt.disable_automasking, \
                "When using predictive_mask, please disable automasking with --disable_automasking"

            # Our implementation of the predictive masking baseline has the the same architecture
            # as our depth decoder. We predict a separate mask for each source frame.
            self.models["predictive_mask"] = networks.DepthDecoder(
                self.models["encoder"].num_ch_enc, self.opt.scales,
                num_output_channels=(len(self.opt.frame_ids) - 1))
            self.models["predictive_mask"].to(self.device)
            self.parameters_to_train += list(self.models["predictive_mask"].parameters())

        self.model_optimizer = optim.Adam(self.parameters_to_train, self.opt.learning_rate)

        # from apex import amp
        # model, optimizer = amp.initialize(model, optimizer, opt_level="O1") # 这里是“欧一”，不是“零一”
        # with amp.scale_loss(loss, optimizer) as scaled_loss:
        #     scaled_loss.backward()

        self.model_lr_scheduler = optim.lr_scheduler.StepLR(
            self.model_optimizer, self.opt.scheduler_step_size, 0.1)

        if self.opt.load_weights_folder is not None:
            self.load_model()

        print("Training model named:\n  ", self.opt.model_name)
        print("Models and tensorboard events files are saved to:\n  ", self.opt.log_dir)
        print("Training is using:\n  ", self.device)

        # data
        datasets_dict = {"kitti": datasets.KITTIRAWDataset,
                         "kitti_odom": datasets.KITTIOdomDataset, 
                         "TUM": datasets.TUMRGBDDataset}
        self.dataset = datasets_dict[self.opt.dataset]

        fpath = os.path.join(os.path.dirname(__file__), "splits", self.opt.split, "{}_files.txt")

        train_filenames = readlines(fpath.format("train"))
        val_filenames = readlines(fpath.format("val"))
        img_ext = '.png' if self.opt.png else '.jpg'

        num_train_samples = len(train_filenames)
        self.num_total_steps = num_train_samples // self.opt.batch_size * self.opt.num_epochs

        train_dataset = self.dataset(
            self.opt.data_path, train_filenames, self.opt.height, self.opt.width,
            self.opt.frame_ids, 4, is_train=True, img_ext=img_ext)
        # self.train_loader = DataLoader(
        #     train_dataset, self.opt.batch_size, True,
        #     num_workers=self.opt.num_workers, pin_memory=True, drop_last=True)
        self.train_loader = DataLoader(
            train_dataset, self.opt.batch_size, True,
            num_workers=self.opt.num_workers, pin_memory=True, drop_last=True, collate_fn=my_collate_fn)
        val_dataset = self.dataset(
            self.opt.data_path, val_filenames, self.opt.height, self.opt.width,
            self.opt.frame_ids, 4, is_train=False, img_ext=img_ext)
        # self.val_loader = DataLoader(
        #     val_dataset, self.opt.batch_size, True,
        #     num_workers=self.opt.num_workers, pin_memory=True, drop_last=True)
        self.val_loader = DataLoader(
            val_dataset, self.opt.batch_size, True,
            num_workers=self.opt.num_workers, pin_memory=True, drop_last=True, collate_fn=my_collate_fn)
        self.val_iter = iter(self.val_loader)

        self.writers = {}
        self.ctime = time.ctime()
        for mode in ["train", "val"]:
            # self.writers[mode] = SummaryWriter(os.path.join(self.log_path, mode))
            self.writers[mode] = SummaryWriter(os.path.join(self.log_path, mode + '_' + self.ctime))

        if not self.opt.no_ssim:
            self.ssim = SSIM()
            self.ssim.to(self.device)

        self.backproject_depth = {}
        self.project_3d = {}
        for scale in self.opt.scales:
            h = self.opt.height // (2 ** scale)
            w = self.opt.width // (2 ** scale)

            self.backproject_depth[scale] = BackprojectDepth(self.opt.batch_size, h, w)
            self.backproject_depth[scale].to(self.device)

            self.project_3d[scale] = Project3D(self.opt.batch_size, h, w)
            self.project_3d[scale].to(self.device)

        self.depth_metric_names = [
            "de/abs_rel", "de/sq_rel", "de/rms", "de/log_rms", "da/a1", "da/a2", "da/a3"]

        print("Using split:\n  ", self.opt.split)
        print("There are {:d} training items and {:d} validation items\n".format(
            len(train_dataset), len(val_dataset)))

        self.save_opts()

    def set_train(self):
        """Convert all models to training mode
        """
        for m in self.models.values():
            m.train()

        self.train_flag = True

    def set_eval(self):
        """Convert all models to testing/evaluation mode
        """
        for m in self.models.values():
            m.eval()

        self.train_flag = False

    def train(self):
        """Run the entire training pipeline
        """
        with torch.autograd.set_detect_anomaly(True):
            self.epoch = 0
            self.step = 0
            self.start_time = time.time()
            for self.epoch in range(self.opt.num_epochs):
                self.run_epoch()
                if (self.epoch + 1) % self.opt.save_frequency == 0:
                    self.save_model()

    def run_epoch(self):
        """Run a single epoch of training and validation
        """

        print("Training")
        self.set_train()

        for batch_idx, inputs in enumerate(self.train_loader):

            before_op_time = time.time()
            # if batch_idx < 20000:
            #     self.geo_scale = 1
            # elif batch_idx < 40000:
            #     self.geo_scale = 0.5
            # else:
            #     self.geo_scale = 0.1
            self.geo_scale = 0.1
            self.show_range = batch_idx % 1000 == 0

            outputs, losses = self.process_batch(inputs)

            ## ZMH: commented and use effective_batch instead
            ## https://medium.com/@davidlmorton/increasing-mini-batch-size-without-increasing-memory-6794e10db672
            # self.model_optimizer.zero_grad()
            # ## losses['loss_cvo/hsv_tog_xyz_ori_'] or 'loss_cos/hsv_tog_xyz_tog_'
            # losses["loss"].backward()
            # self.model_optimizer.step()

            if batch_idx > 0 and batch_idx % self.opt.iters_per_update == 0:
                    self.model_optimizer.step()
                    self.model_optimizer.zero_grad()
                    # print('optimizer update at', iter_overall)
            if self.opt.cvo_as_loss:
                # loss = losses["loss_cos/hsv_tog_xyz_tog_"] / self.opt.iters_per_update
                # loss = ( losses["loss_cvo/hsv_ori_xyz_ori__2"] + losses["loss_cvo/hsv_ori_xyz_ori__3"] )/2 / self.opt.iters_per_update
                # loss = ( losses["loss_inp/hsv_ori_xyz_ori__2"] + losses["loss_inp/hsv_ori_xyz_ori__3"] )/2 / self.opt.iters_per_update
                # loss = ( losses["loss_inp/hsv_ori_xyz_ori__0"] + losses["loss_inp/hsv_ori_xyz_ori__1"] + losses["loss_inp/hsv_ori_xyz_ori__2"] + losses["loss_inp/hsv_ori_xyz_ori__3"] ) / self.opt.iters_per_update
                loss = ( losses["loss_inp/xyz_ori__0"] + losses["loss_inp/xyz_ori__1"] + losses["loss_inp/xyz_ori__2"] + losses["loss_inp/xyz_ori__3"] ) / self.opt.iters_per_update
                # loss = ( losses["loss_cvo/hsv_ori_xyz_ori__0"] + losses["loss_cvo/hsv_ori_xyz_ori__1"] + losses["loss_cvo/hsv_ori_xyz_ori__2"] + losses["loss_cvo/hsv_ori_xyz_ori__3"] ) / self.opt.iters_per_update
            else:
                loss = losses["loss"] / self.opt.iters_per_update
            if self.opt.disp_in_loss:
                loss += 0.1 * (losses["loss_disp/0"]+ losses["loss_disp/1"] + losses["loss_disp/2"] + losses["loss_disp/3"]) / self.num_scales / self.opt.iters_per_update
            if self.opt.supervised_by_gt_depth:
                # loss += 0.1 * losses["loss_cos/sum"] / self.num_scales / self.opt.iters_per_update
                loss += 1e-6 * losses["loss_inp/sum"] / self.num_scales / self.opt.iters_per_update
            if self.opt.sup_cvo_pose_lidar:
                loss += 0.1 * losses["loss_pose/cos_sum"] / self.num_scales / self.opt.iters_per_update
            loss.backward()


            duration = time.time() - before_op_time

            # log less frequently after the first 2000 steps to save time & disk space
            early_phase = batch_idx % self.opt.log_frequency == 0 and self.step < 2000
            late_phase = self.step % 2000 == 0

            if early_phase or late_phase:
                self.log_time(batch_idx, duration, losses["loss"].cpu().data)

                if "depth_gt" in inputs:
                    self.compute_depth_losses(inputs, outputs, losses)

                self.log("train", inputs, outputs, losses)
                self.val()

            self.step += 1
        
        self.model_lr_scheduler.step()

    def process_batch(self, inputs):
        """Pass a minibatch through the network and generate images and losses
        """
        for key, ipt in inputs.items():
            if "velo_gt" not in key:
                inputs[key] = ipt.to(self.device)
            else:
                inputs[key] = [ipt_i.to(self.device) for ipt_i in ipt]

        if self.opt.pose_model_type == "shared":
            # If we are using a shared encoder for both depth and pose (as advocated
            # in monodepthv1), then all images are fed separately through the depth encoder.
            all_color_aug = torch.cat([inputs[("color_aug", i, 0)] for i in self.opt.frame_ids])
            all_features = self.models["encoder"](all_color_aug)
            all_features = [torch.split(f, self.opt.batch_size) for f in all_features]

            features = {}
            for i, k in enumerate(self.opt.frame_ids):
                features[k] = [f[i] for f in all_features]

            outputs = self.models["depth"](features[0])
        else:
            # Otherwise, we only feed the image with frame_id 0 through the depth encoder
            features = self.models["encoder"](inputs["color_aug", 0, 0])
            outputs = self.models["depth"](features)
            ### ZMH: outputs have disp image of different scales

            ## ZMH: switch
            outputs_others = None
            ## ZMH: predict depth for each image (other than the host image)
            features_others = {} # initialize a dict
            outputs_others = {}
            for i in self.opt.frame_ids:
                if i == 0:
                    continue
                features_others[i] = self.models["encoder"](inputs["color_aug", i, 0])
                outputs_others[i] = self.models["depth"](features_others[i] )


        if self.opt.predictive_mask:
            outputs["predictive_mask"] = self.models["predictive_mask"](features)
            ## ZMH: process depth for each image
            if outputs_others is not None:
                for i in self.opt.frame_ids:
                    if i == 0:
                        continue
                    outputs_others[i]["predictive_mask"] = self.models["predictive_mask"](features_others[i])

        if self.use_pose_net:
            outputs.update(self.predict_poses(inputs, features))

        # self.generate_images_pred(inputs, outputs)
        self.generate_images_pred(inputs, outputs, outputs_others)
        losses = self.compute_losses(inputs, outputs, outputs_others)

        return outputs, losses

    def predict_poses(self, inputs, features):
        """Predict poses between input frames for monocular sequences.
        """
        outputs = {}
        if self.num_pose_frames == 2:
            # In this setting, we compute the pose to each source frame via a
            # separate forward pass through the pose network.

            # select what features the pose network takes as input
            if self.opt.pose_model_type == "shared":
                pose_feats = {f_i: features[f_i] for f_i in self.opt.frame_ids}
            else:
                pose_feats = {f_i: inputs["color_aug", f_i, 0] for f_i in self.opt.frame_ids}

            for f_i in self.opt.frame_ids[1:]:
                if f_i != "s":
                    # To maintain ordering we always pass frames in temporal order
                    if f_i < 0:
                        pose_inputs = [pose_feats[f_i], pose_feats[0]]
                    else:
                        pose_inputs = [pose_feats[0], pose_feats[f_i]]

                    if self.opt.pose_model_type == "separate_resnet":
                        pose_inputs = [self.models["pose_encoder"](torch.cat(pose_inputs, 1))]
                    elif self.opt.pose_model_type == "posecnn":
                        pose_inputs = torch.cat(pose_inputs, 1)

                    axisangle, translation = self.models["pose"](pose_inputs)
                    outputs[("axisangle", 0, f_i)] = axisangle
                    outputs[("translation", 0, f_i)] = translation

                    # Invert the matrix if the frame id is negative
                    outputs[("cam_T_cam", 0, f_i)] = transformation_from_parameters(
                        axisangle[:, 0], translation[:, 0], invert=(f_i < 0))

        else:
            # Here we input all frames to the pose net (and predict all poses) together
            if self.opt.pose_model_type in ["separate_resnet", "posecnn"]:
                pose_inputs = torch.cat(
                    [inputs[("color_aug", i, 0)] for i in self.opt.frame_ids if i != "s"], 1)

                if self.opt.pose_model_type == "separate_resnet":
                    pose_inputs = [self.models["pose_encoder"](pose_inputs)]

            elif self.opt.pose_model_type == "shared":
                pose_inputs = [features[i] for i in self.opt.frame_ids if i != "s"]

            axisangle, translation = self.models["pose"](pose_inputs)

            for i, f_i in enumerate(self.opt.frame_ids[1:]):
                if f_i != "s":
                    outputs[("axisangle", 0, f_i)] = axisangle
                    outputs[("translation", 0, f_i)] = translation
                    outputs[("cam_T_cam", 0, f_i)] = transformation_from_parameters(
                        axisangle[:, i], translation[:, i])

        return outputs

    def val(self):
        """Validate the model on a single minibatch
        """
        self.set_eval()
        try:
            inputs = self.val_iter.next()
        except StopIteration:
            self.val_iter = iter(self.val_loader)
            inputs = self.val_iter.next()

        with torch.no_grad():
            outputs, losses = self.process_batch(inputs)

            if "depth_gt" in inputs:
                self.compute_depth_losses(inputs, outputs, losses)

            self.log("val", inputs, outputs, losses)
            del inputs, outputs, losses

        self.set_train()

    ### ZMH: make it a function to be repeated for images other than index 0
    def from_disp_to_depth(self, disp, scale, force_multiscale=False):
        """ZMH: generate depth of original scale unless self.opt.v1_multiscale
        """
        ## ZMH: force_multiscale option added by me to adapt to cases where we want multiscale depth
        if self.opt.v1_multiscale or force_multiscale:
            source_scale = scale
        else:
            disp = F.interpolate(
                disp, [self.opt.height, self.opt.width], mode="bilinear", align_corners=False)
            source_scale = 0

        _, depth = disp_to_depth(disp, self.opt.min_depth, self.opt.max_depth)
        return depth, source_scale

    def gen_pcl_gt(self, inputs, outputs, disp, scale, frame_id, T_inv=None):
        ### ZMH: for host frame, T_inv is None.
        ### ZMH: gt depth -> point cloud gt (for other frames, transform the point cloud to host frame)
        ### ZMH: Due to that ground truth depth image is not valid at every pixel, the length of pointcloud in the mini-batch is not consistent. 
        ### ZMH: Therefore samples are processed one by one in the mini-batch.
        cam_points_gt, masks = self.backproject_depth[scale](
            inputs[("depth_gt_scale", frame_id, scale)], inputs[("inv_K", scale)], separate=True)
        if T_inv is None:
            outputs[("xyz_gt", frame_id, scale)] = cam_points_gt
            rgb_gt = {}
            for ib in range(self.opt.batch_size):
                color = inputs[("color", frame_id, scale)][ib]
                color = color.view(1, 3, -1)
                color_sel = color[..., masks[ib]]
                rgb_gt[ib] = color_sel
            outputs[("rgb_gt", frame_id, scale)] = rgb_gt
        else:
            cam_points_other_gt_in_host = {}
            rgb_other_gt = {}
            for ib in range(self.opt.batch_size):
                T_inv_i = T_inv[ib:ib+1]
                cam_points_other_gt_in_host[ib] = torch.matmul(T_inv_i, cam_points_gt[ib] )
                color = inputs[("color", frame_id, scale)][ib]
                color = color.view(1, 3, -1)
                color_sel = color[..., masks[ib]]
                rgb_other_gt[ib] = color_sel
            outputs[("xyz_gt", frame_id, scale)] = cam_points_other_gt_in_host
            outputs[("rgb_gt", frame_id, scale)] = rgb_other_gt

        ### ZMH: disparity prediction -> depth prediction -> point cloud prediction (for other frames, transform the point cloud to host frame)
        depth_curscale, _ = self.from_disp_to_depth(disp, scale, force_multiscale=True)
        cam_points_curscale = self.backproject_depth[scale](
            depth_curscale, inputs[("inv_K", scale)])
        if T_inv is None:
            xyz_is = {}
            rgb_is = {}
            for ib in range(self.opt.batch_size):
                xyz_is[ib] = cam_points_curscale[ib:ib+1, ..., masks[ib]]
                rgb_is[ib] = inputs[("color", frame_id, scale)][ib].view(1,3,-1)[..., masks[ib]]
            # outputs[("xyz_in_host", 0, scale)] = cam_points_curscale
            outputs[("xyz_in_host", frame_id, scale)] = xyz_is
            outputs[("rgb_in_host", frame_id, scale)] = rgb_is
        else:
            outputs[("depth", frame_id, scale)] = depth_curscale
            ### ZMH: transform points in source frame to host frame
            cam_points_other_in_host = torch.matmul(T_inv, cam_points_curscale)
            ### ZMH: log the 3d points to output (points in source frame transformed to host frame)
            ### ZMH: to sample the points only at where gt are avaiable: 
            xyz_is = {}
            rgb_is = {}
            for ib in range(self.opt.batch_size):
                xyz_is[ib] = cam_points_other_in_host[ib:ib+1, ..., masks[ib]]
                rgb_is[ib] = inputs[("color", frame_id, scale)][ib].view(1,3,-1)[..., masks[ib]]
            # outputs[("xyz_in_host", frame_id, scale)] = cam_points_other_in_host
            outputs[("xyz_in_host", frame_id, scale)] = xyz_is
            outputs[("rgb_in_host", frame_id, scale)] = rgb_is

    def gen_pcl_wrap_host(self, inputs, outputs, scale):

        ### 1. gt from lidar
        cam_points_gt, masks = self.backproject_depth[scale](
            inputs[("depth_gt_scale", 0, scale)], inputs[("inv_K", scale)], separate=True)
        outputs[("xyz_gt", 0, scale)] = cam_points_gt
        rgb_gt = {}
        for ib in range(self.opt.batch_size):
            color = inputs[("color", 0, scale)][ib]
            color = color.view(1, 3, -1)
            color_sel = color[..., masks[ib]]
            rgb_gt[ib] = color_sel
        outputs[("rgb_gt", 0, scale)] = rgb_gt

        ### 2. host frame same sampling
        masks = inputs[("depth_mask", 0, scale)]
        masks = [masks[i].view(-1) for i in range(masks.shape[0]) ]

        cam_points_host = self.backproject_depth[scale](
            outputs[("depth_wrap", 0, scale)], inputs[("inv_K", scale)] )
        xyz_is = {}
        rgb_is = {}
        for ib in range(self.opt.batch_size):
            xyz_is[ib] = cam_points_host[ib:ib+1, ..., masks[ib]]
            rgb_is[ib] = inputs[("color", 0, scale)][ib].view(1,3,-1)[..., masks[ib]]
        outputs[("xyz_in_host", 0, scale)] = xyz_is
        outputs[("rgb_in_host", 0, scale)] = rgb_is
        return masks

    def gen_pcl_wrap_other(self, inputs, outputs, scale, frame_id, T_inv, masks):
        ### 3. host frame by wrapping from adjacent frame
        uv_wrap = outputs[("uv_wrap", frame_id, scale)].view(self.opt.batch_size, 2, -1)
        ones_ =  torch.ones((self.opt.batch_size, 1, uv_wrap.shape[2]), dtype=uv_wrap.dtype, device=uv_wrap.device)
        own_id_coords = torch.cat((uv_wrap, 
                ones_), dim=1) # B*3*N
        
        cam_points_wrap = self.backproject_depth[scale](
            outputs[("depth_wrap", frame_id, scale)], inputs[("inv_K", scale)], own_pix_coords=own_id_coords )
        cam_points_other_in_host = torch.matmul(T_inv, cam_points_wrap)
        xyz_is = {}
        rgb_is = {}
        for ib in range(self.opt.batch_size):
            xyz_is[ib] = cam_points_other_in_host[ib:ib+1, ..., masks[ib]]
            rgb_is[ib] = outputs[("color_wrap", frame_id, scale)][ib].view(1,3,-1)[..., masks[ib]]
        outputs[("xyz_in_host", frame_id, scale)] = xyz_is
        outputs[("rgb_in_host", frame_id, scale)] = rgb_is

        ### 4. generate gt for adjacent frames
        cam_points_gt, masks = self.backproject_depth[scale](
            inputs[("depth_gt_scale", frame_id, scale)], inputs[("inv_K", scale)], separate=True)
        # cam_points_gt_in_host = torch.matmul(T_inv, cam_points_gt)
        # outputs[("xyz_gt", frame_id, scale)] = cam_points_gt_in_host
        outputs[("xyz_gt", frame_id, scale)] = cam_points_gt
        rgb_gt = {}
        for ib in range(self.opt.batch_size):
            color = inputs[("color", frame_id, scale)][ib]
            color = color.view(1, 3, -1)
            color_sel = color[..., masks[ib]]
            rgb_gt[ib] = color_sel
        outputs[("rgb_gt", frame_id, scale)] = rgb_gt

    def get_xyz_dense(self, frame_id, scale, inputs, outputs):
        cam_points_gt = self.backproject_depth[scale](
            inputs[("depth_gt_scale", frame_id, scale)], inputs[("inv_K", scale)], as_img=True)

        if frame_id == 0:
            cam_points_pred = self.backproject_depth[scale](
                outputs[("depth_wrap", frame_id, scale)], inputs[("inv_K", scale)], as_img=True)
        else:
            cam_points_pred = self.backproject_depth[scale](
                outputs[("depth", frame_id, scale)], inputs[("inv_K", scale)], as_img=True)

        outputs[("xyz1_dense_gt", frame_id, scale)] = cam_points_gt
        outputs[("xyz1_dense_pred", frame_id, scale)] = cam_points_pred

    def get_xyz_rgb_pair(self, frame_id, scale, inputs, outputs, gt):
        if gt:
            xyz1 = outputs[("xyz1_dense_gt", frame_id, scale)]
            mask = inputs[("depth_mask_gt", frame_id, scale)]
        else:
            xyz1 = outputs[("xyz1_dense_pred", frame_id, scale)]
            mask = inputs[("depth_mask", frame_id, scale)]

        rgb = inputs[("color", frame_id, scale)]
        hsv = rgb_to_hsv(rgb, flat=False)
        # print("hsv shape", hsv.shape)

        return xyz1, hsv, mask
    
    def get_xyz_aligned(self, id_pair, xyz1, outputs):
        xyz_aligned = [None]*2

        if id_pair[0] == id_pair[1]:
            xyz_aligned[0] = xyz1[0][:,:3]
        elif id_pair[0] == 0 and id_pair[1] != 0:
            ## TODO: Here other modes of pose prediction is not included yet.
            T = outputs[("cam_T_cam", 0, id_pair[1])] # T_x0
            height_cur = xyz1[0].shape[2]
            width_cur = xyz1[0].shape[3]
            xyz1_flat = xyz1[0].view(self.opt.batch_size, 4, -1)
            xyz1_trans = torch.matmul(T, xyz1_flat)[:,:3]
            xyz_aligned[0] = xyz1_trans.view(self.opt.batch_size, 3, height_cur, width_cur)
        else:
            raise ValueError("id_pair [{}, {}] not recognized".format(id_pair[0], id_pair[1]) )

        xyz_aligned[1] = xyz1[1][:,:3]
        return xyz_aligned

    def calc_inp_from_dense(self, xyz_pair, mask_pair, hsv_pair=None):
        xyz_ell = self.geo_scale
        hsv_ell = 0.4

        half_w = 4
        half_h = 4
        
        height = xyz_pair[0].shape[2]
        width = xyz_pair[1].shape[3]
        # zeros_dummy = torch.zeros((self.opt.batch_size, 1, height, width), device=self.device, dtype=xyz_pair[0].dtype)
        # exp_sum = torch.tensor(0, device=self.device, dtype=xyz_pair[0].dtype)

        num_off = (2*half_w+1) * (2*half_h+1)

        xyz_pair_off = torch.zeros((self.opt.batch_size, 3, num_off, height, width), device=self.device, dtype=xyz_pair[0].dtype)
        mask_pair_off = torch.zeros((self.opt.batch_size, 1, num_off, height, width), device=self.device, dtype=torch.bool)
        
        if hsv_pair is not None:
            hsv_pair_off = torch.zeros((self.opt.batch_size, 3, num_off, height, width), device=self.device, dtype=hsv_pair[0].dtype)

        for i in range(-half_h, half_h):
            # idx_dest_row = []
            # idx_dest_col = []
            # idx_from_row = []
            # idx_from_col = []
            for j in range(-half_w, half_w):
                off_h0_start = max(0, -i)
                off_h0_end = min(0, -i)
                off_w0_start = max(0, -j)
                off_w0_end = min(0, -j)
                off_h1_start = -off_h0_end
                off_h1_end = -off_h0_start
                off_w1_start = -off_w0_end
                off_w1_end = -off_w0_start
                

                idx = (i + half_h) * (2 * half_w + 1) + j + half_w
                xyz_pair_off[:,:,idx, off_h0_start : height+off_h0_end, off_w0_start : width+off_w0_end] = xyz_pair[1][:,:,off_h1_start : height+off_h1_end, off_w1_start : width+off_w1_end]
                mask_pair_off[:,:,idx, off_h0_start : height+off_h0_end, off_w0_start : width+off_w0_end] = mask_pair[1][:,:,off_h1_start : height+off_h1_end, off_w1_start : width+off_w1_end]
                if hsv_pair is not None:
                    hsv_pair_off[:,:,idx, off_h0_start : height+off_h0_end, off_w0_start : width+off_w0_end] = hsv_pair[1][:,:,off_h1_start : height+off_h1_end, off_w1_start : width+off_w1_end]

                # diff_xyz = torch.zeros((self.opt.batch_size, 1, height, width), device=self.device, dtype=xyz_pair[0].dtype)
                # diff_xyz[:,:,off_h0_start : height+off_h0_end, off_w0_start : width+off_w0_end] = \
                #     torch.pow(torch.norm(xyz_pair[0][:,:,off_h0_start : height+off_h0_end, off_w0_start : width+off_w0_end] - \
                #         xyz_pair[1][:,:,off_h1_start : height+off_h1_end, off_w1_start : width+off_w1_end], dim=1 ), 2)
                # exp_xyz = torch.exp(-diff_xyz / (2*xyz_ell*xyz_ell))
                # exp = exp_xyz

                # if hsv_pair is not None:
                #     diff_hsv = torch.zeros((self.opt.batch_size, 1, height, width), device=self.device, dtype=hsv_pair[0].dtype)
                #     diff_hsv[:,:,off_h0_start : height+off_h0_end, off_w0_start : width+off_w0_end] = \
                #         torch.pow(torch.norm(hsv_pair[0][:,:,off_h0_start : height+off_h0_end, off_w0_start : width+off_w0_end] - \
                #             hsv_pair[1][:,:,off_h1_start : height+off_h1_end, off_w1_start : width+off_w1_end], dim=1 ), 2)
                #     exp_hsv = torch.exp(-diff_hsv / (2*hsv_ell*hsv_ell))
                #     exp = torch.mul(exp_xyz, exp_hsv)

                # mask = mask_pair[0].clone()
                # mask[:,:,off_h0_start : height+off_h0_end, off_w0_start : width+off_w0_end] = \
                #     mask[:,:,off_h0_start : height+off_h0_end, off_w0_start : width+off_w0_end] & \
                #         mask_pair[1][:,:,off_h1_start : height+off_h1_end, off_w1_start : width+off_w1_end]

                # exp = torch.where(mask, exp, zeros_dummy)
                # exp_sum = exp_sum + exp.sum()
        
        diff_xyz = torch.exp( -torch.pow(torch.norm(xyz_pair[0].unsqueeze(2) - xyz_pair_off, dim=1), 2) / (2*xyz_ell*xyz_ell) )
        diff = diff_xyz
        if hsv_pair is not None:
            diff_hsv = torch.exp( -torch.pow(torch.norm(hsv_pair[0].unsqueeze(2) - hsv_pair_off, dim=1), 2) / (2*hsv_ell*hsv_ell) )
            diff = torch.mul(diff_xyz, diff_hsv)

        zeros_dummy = torch.zeros_like(diff)
        diff = torch.where(mask_pair_off, diff, zeros_dummy)

        exp_mask_halfsum = diff.sum(dim=2)
        zeros_dummy = torch.zeros_like(exp_mask_halfsum)
        exp_mask_halfsum = torch.where(mask_pair[0], exp_mask_halfsum, zeros_dummy)

        exp_sum = exp_mask_halfsum.sum()

        return exp_sum 

    def gen_innerp_dense(self, inputs, outputs):
        id_pairs = [(0,0), (1,1), (-1,-1), (0,-1), (0,1)]
        gt_pairs = [(True, True), (True, False), (False, False)]
        innerps = {}
        for id_pair in id_pairs: #self.opt.frame_ids:
            # i, j = id_pair
            for scale in self.opt.scales:
                for gt_pair in gt_pairs:
                    xyz1 = [None] * 2
                    hsv = [None] * 2
                    mask = [None]*2
                    for k in range(2):
                        xyz1[k], hsv[k], mask[k] = self.get_xyz_rgb_pair(id_pair[k], scale, inputs, outputs, gt=gt_pair[k])
                    xyz_aligned = self.get_xyz_aligned(id_pair, xyz1, outputs)
                    
                    innerps[(id_pair, scale, gt_pair)] = self.calc_inp_from_dense(xyz_aligned, mask, hsv)
                    
        return innerps

    def gen_cvo_loss_dense(self, innerps, id_pair, scale, gt_pair):
        i,j = id_pair
        gt_i, gt_j = gt_pair
        inp = innerps[(id_pair, scale, gt_pair)]
        if i == j:
            f_dist = torch.tensor(0, device=self.device, dtype=inp.dtype)
            cos_sim = torch.tensor(0, device=self.device, dtype=inp.dtype)
        else:
            inp_ii = innerps[((i,i), scale, (gt_i, gt_i))]
            inp_jj = innerps[((j,j), scale, (gt_j, gt_j))]
            f_dist = inp_ii + inp_jj - 2 * inp
            cos_sim = 1 - inp/torch.sqrt(inp_ii * inp_jj)
        
        return inp, f_dist, cos_sim

    # def generate_images_pred(self, inputs, outputs):
    ## ZMH: add depth output of other images
    def generate_images_pred(self, inputs, outputs, outputs_others=None):
        """Generate the warped (reprojected) color images for a minibatch.
        Generated images are saved into the `outputs` dictionary.
        """
        for scale in self.opt.scales:
            ### ZMH: generate depth of host image from current scale disparity estimation
            disp = outputs[("disp", scale)]
            depth, source_scale = self.from_disp_to_depth(disp, scale)
            outputs[("depth", 0, scale)] = depth
            cam_points = self.backproject_depth[source_scale](
                depth, inputs[("inv_K", source_scale)])

            if self.opt.cvo_loss:
                # self.gen_pcl_gt(inputs, outputs, disp, scale, 0)
                depth_curscale, _ = self.from_disp_to_depth(disp, scale, force_multiscale=True)
                outputs[("depth_wrap", 0, scale)] = depth_curscale
                cam_points_curscale = self.backproject_depth[scale](
                    depth_curscale, inputs[("inv_K", scale)])
                
                if self.opt.cvo_loss_dense:
                    self.get_xyz_dense(0, scale, inputs, outputs)
                else:
                    masks = self.gen_pcl_wrap_host(inputs, outputs, scale)

            ### ZMH: the neighboring images (either the stereo counterpart or the previous/next image)
            for i, frame_id in enumerate(self.opt.frame_ids[1:]):

                if frame_id == "s":
                    T = inputs["stereo_T"]
                else:
                    T = outputs[("cam_T_cam", 0, frame_id)]

                # from the authors of https://arxiv.org/abs/1712.00175
                if self.opt.pose_model_type == "posecnn":

                    axisangle = outputs[("axisangle", 0, frame_id)]
                    translation = outputs[("translation", 0, frame_id)]

                    inv_depth = 1 / depth
                    mean_inv_depth = inv_depth.mean(3, True).mean(2, True)

                    T = transformation_from_parameters(
                        axisangle[:, 0], translation[:, 0] * mean_inv_depth[:, 0], frame_id < 0)

                T_inv = torch.inverse(T)
                
                if self.show_range and scale == self.opt.scales[0]:
                    print("frame", frame_id, "\n", T)

                ## ZMH: what here it is doing: 
                ## using depth of host frame 0, reprojecting to another frame i, reconstruct host frame 0 using reprojection coords and frame i's pixels.
                ## The difference between true host frame 0 and reconstructed host frame 0 is the photometric error

                ### ZMH: px = T_x0 *p0
                ### ZMH: therefore the T is the pose of host relative to frame x
                pix_coords = self.project_3d[source_scale](
                    cam_points, inputs[("K", source_scale)], T)

                outputs[("sample", frame_id, scale)] = pix_coords

                outputs[("color", frame_id, scale)] = F.grid_sample(
                    inputs[("color", frame_id, source_scale)],
                    outputs[("sample", frame_id, scale)],
                    padding_mode="border")
                ## ZMH: generate depth of other images
                if self.opt.cvo_loss:# and frame_id == -1:

                    assert outputs_others is not None, "no disparity prediction of other images!"
                    disp = outputs_others[frame_id][("disp", scale)]
                    
                    # self.gen_pcl_gt(inputs, outputs, disp, scale, frame_id, T_inv) 
                    depth_curscale, _ = self.from_disp_to_depth(disp, scale, force_multiscale=True)
                    outputs[("depth", frame_id, scale)] = depth_curscale         

                    if self.opt.cvo_loss_dense:
                        self.get_xyz_dense(frame_id, scale, inputs, outputs)

                    else:
                        pix_coords_curscale = self.project_3d[scale](
                            cam_points_curscale, inputs[("K", scale)], T)
                        outputs[("sample_wrap", frame_id, scale)] = pix_coords_curscale

                        outputs[("depth_wrap", frame_id, scale)] = F.grid_sample(
                            outputs[("depth", frame_id, scale)],
                            outputs[("sample_wrap", frame_id, scale)],
                            padding_mode="border")

                        outputs[("color_wrap", frame_id, scale)] = F.grid_sample(
                            inputs[("color", frame_id, scale)],
                            outputs[("sample_wrap", frame_id, scale)],
                            padding_mode="border")

                        outputs[("uv_wrap", frame_id, scale)] = F.grid_sample(
                            self.backproject_depth[scale].id_coords.unsqueeze(0).expand(self.opt.batch_size, -1, -1, -1),
                            outputs[("sample_wrap", frame_id, scale)],
                            padding_mode="border")
                        
                        self.gen_pcl_wrap_other(inputs, outputs, scale, frame_id, T_inv, masks)

                    # ### ZMH: transform the points in host frame to source frame
                    # cam_pts_trans = torch.matmul(T, cam_points)
                    # ### ZMH: Flatten color matrix
                    # color_ori = inputs["color_aug", 0, 0].view(cam_points.shape[0], 3, -1)
                    # color_other = inputs["color_aug", frame_id, 0].view(cam_points.shape[0], 3, -1)
                    # print(cam_points.shape) # B*4*N
                    # print(cam_points.dtype)
                    # draw3DPts(cam_pts_trans.detach()[:,:3,:], pcl_2=cam_points_other.detach()[:,:3,:], color_1=color_ori.detach(), color_2=color_other.detach())

                    # ### ZMH: visualize the grount truth point cloud
                    # for ib in range(self.opt.batch_size):
                    #     draw3DPts(outputs[("xyz_gt", 0, 0)][ib].detach()[:,:3,:], pcl_2=outputs[("xyz_gt", frame_id, scale)][ib].detach()[:,:3,:], 
                    #         color_1=outputs[("rgb_gt", 0, 0)][ib].detach(), color_2=outputs[("rgb_gt", frame_id, scale)][ib].detach())
                    

                if not self.opt.disable_automasking:
                    outputs[("color_identity", frame_id, scale)] = \
                        inputs[("color", frame_id, source_scale)]

    # def func_inner_prod(self, gramian_p, gramian_c):
    #     """
    #     Calculate the inner product of two functions from gramian matrix of two point clouds
    #     """
    #     prod = torch.sum(gramian_p * gramian_c)
    #     return prod

    def loss_from_inner_prod(self, inner_prods):
        f1_f2_dist = inner_prods[(0,0)] + inner_prods[(1,1)] - 2 * inner_prods[(0,1)]
        cos_similarity = 1 - inner_prods[(0,1)] / torch.sqrt( inner_prods[(0,0)] * inner_prods[(1,1)] )

        return f1_f2_dist, cos_similarity

    def inner_prod_from_gramian(self, gramians ):
        """
        Calculate function distance loss and cosine similarity by multiplying the gramians in all domains together for a specific pair
        """
        inner_prods = {}
        list_of_ij = [(0,0), (1,1), (0,1)]

        for ij in list_of_ij:
            gramian_list = [gramian[ij] for gramian in gramians.values() ]
            # gramian_stack = torch.stack(gramian_list, dim=3)
            # inner_prods[ij] = torch.sum(torch.prod(gramian_stack, dim=3))
            if len(gramian_list) == 1:
                inner_prods[ij] = torch.sum(gramian_list[0])
            else:
                inner_p = gramian_list[0] * gramian_list[1]
                for k in range(2,len(gramian_list)):
                    inner_p = inner_p * gramian_list[k]
                inner_prods[ij] = torch.sum(inner_p)
            if self.opt.normalize_inprod_over_pts:
                inner_prods[ij] = inner_prods[ij] / (gramian_list[0].shape[1] * gramian_list[0].shape[2])

        return inner_prods

    def cvo_gramian(self, vectors, dist_coef, normalize_mode="ori"):
        """
        Compute the gramian matix for a pair of vectors in a specific domain (xyz, hsv, etc.)
        Output: {(0,0): _, (1,1): _, (0,1): _}
        """
        # B*C*N
        vec_local = {}
        vec_local[0] = vectors[0]
        vec_local[1] = vectors[1]
        
        # print(vec_local[0].std(dim=2, keepdim=True) )
        # print(vec_local[1].std(dim=2, keepdim=True) )
        
        if normalize_mode == "sep":
            vec_local[0] = vec_local[0] - vec_local[0].mean(dim=2, keepdim=True).expand_as(vec_local[0])
            vec_local[0] = vec_local[0] / vec_local[0].std(dim=2, keepdim=True).expand_as(vec_local[0])
            vec_local[1] = vec_local[1] - vec_local[1].mean(dim=2, keepdim=True).expand_as(vec_local[1])
            vec_local[1] = vec_local[1] / vec_local[1].std(dim=2, keepdim=True).expand_as(vec_local[1])
        elif normalize_mode == "tog":
            v12 = torch.cat((vec_local[0], vec_local[1]), dim=2)
            v12 = v12 - v12.mean(dim=2, keepdim=True).expand_as(v12)
            v12 = v12 / v12.std(dim=2, keepdim=True).expand_as(v12)
            vec_local[0] = v12[:,:,:vec_local[0].shape[2]]
            vec_local[1] = v12[:,:,vec_local[0].shape[2]:]

        gramians = {}
        list_of_ij = [(0,0), (1,1), (0,1)]
        for ij in list_of_ij:
            (i,j) = ij
            gramians[ij] = kern_mat(vec_local[i], vec_local[j], dist_coef)
        
        return gramians

    def compute_cvo_loss_with_options(self, vector_to_cvo, items_to_cal_gram, dist_coefs, norm_tags):

        # ### A way to save memory: 
        # ### Calculate all gramians altogether. 
        # thre_t = 8.315e-3
        # # thre_d = -2.0 * dist_coef * dist_coef * np.log(thre_t)
        # inner_prods = {}
        # ij_list = [(0,0), (1,1), (0,1)]
        # for ij in ij_list:
        #     i,j  = ij
        #     inner_prods[ij] = torch.zeros([], device=self.device, dtype=torch.float32)
        #     # gramian_all = torch.zeros((1, vector_to_cvo[items_to_cal_gram[0]][0].shape[2]), device=self.device, dtype=torch.float32)
        #     # zero_dummy = torch.zeros((1, vector_to_cvo[items_to_cal_gram[0]][i].shape[2]), device=self.device, dtype=torch.float32)
        #     for k in range(vector_to_cvo[items_to_cal_gram[0]][j].shape[2]):
        #         gramian_iter = torch.ones((1, vector_to_cvo[items_to_cal_gram[0]][i].shape[2]), device=self.device, dtype=torch.float32)
        #         for item in items_to_cal_gram:
        #             vec0 = vector_to_cvo[item][i]
        #             vec1 = vector_to_cvo[item][j]
        #             diff = torch.norm(vec0 - vec1[..., k:k+1].expand_as(vec0), dim=1)
        #             diff_exp = torch.exp(-torch.pow(diff, 2) / (2*dist_coefs[item]*dist_coefs[item]) )
        #             # diff_exp = torch.where(diff_exp >= thre_t, diff_exp, torch.zeros_like(diff_exp) )
        #             # diff_exp = torch.where(diff_exp >= thre_t, diff_exp, zero_dummy )
        #             gramian_iter = torch.mul(gramian_iter, diff_exp)
        #         inner_prods[ij] = inner_prods[ij] + gramian_iter.sum()

        ### Calculate gramians
        gramians = {}
        for item in items_to_cal_gram: # for item in vector_to_cvo:
            gramians[item] = self.cvo_gramian(vector_to_cvo[item], dist_coefs[item], normalize_mode=norm_tags[item])

        ### Calculate inner product
        inner_prods = self.inner_prod_from_gramian(gramians )


        ### Calculate CVO loss
        f1_f2_dist, cos_similarity = self.loss_from_inner_prod( inner_prods )
        
        return f1_f2_dist, cos_similarity, -inner_prods[(0,1)]

    def name_loss_from_norm_options(self, items_to_cal_gram, norm_tags):
        item_tags = {}
        name_loss = ""
        for item in items_to_cal_gram:
            name_loss = name_loss + item + "_" + norm_tags[item] + "_"
        return name_loss
        
    def compute_cvo_loss(self, vector_to_cvo):

        if "rgb" in vector_to_cvo:
            vector_to_cvo["hsv"] = {}
            for i in range(2):
                vector_to_cvo["hsv"][i] = rgb_to_hsv(vector_to_cvo["rgb"][i], flat=True )
            
        items_to_calculate_gram = ["hsv", "xyz"]
        # items_to_calculate_gram = ["xyz"]

        dist_coefs = {}
        # for item in items_to_calculate_gram:
        #     dist_coefs[item] = 0.1
        dist_coefs["xyz"] = self.geo_scale
        if "rgb" in vector_to_cvo:
            dist_coefs["hsv"] = 0.4

        f1_f2_dist = {}
        cos_similarity = {}
        inner_prod = {}

        norm_tags = {}
        # norm_tags["hsv"] = "tog" # or "tog" "ori"
        # norm_tags["xyz"] = "tog" # or "ori"

        # name_loss = self.name_loss_from_norm_options(items_to_calculate_gram, norm_tags)
        # f1_f2_dist[name_loss], cos_similarity[name_loss] = self.compute_cvo_loss_with_options(vector_to_cvo, items_to_calculate_gram, dist_coefs, norm_tags)

        # norm_tags["hsv"] = "tog" # or "tog" "ori"
        # norm_tags["xyz"] = "ori" # or "ori"

        # name_loss = self.name_loss_from_norm_options(items_to_calculate_gram, norm_tags)
        # f1_f2_dist[name_loss], cos_similarity[name_loss] = self.compute_cvo_loss_with_options(vector_to_cvo, items_to_calculate_gram, dist_coefs, norm_tags)

        if "rgb" in vector_to_cvo:
            norm_tags["hsv"] = "ori" # or "tog" "ori"
        norm_tags["xyz"] = "ori" # or "ori"

        name_loss = self.name_loss_from_norm_options(items_to_calculate_gram, norm_tags)
        f1_f2_dist[name_loss], cos_similarity[name_loss], inner_prod[name_loss] = self.compute_cvo_loss_with_options(vector_to_cvo, items_to_calculate_gram, dist_coefs, norm_tags)

        return f1_f2_dist, cos_similarity, inner_prod

    def compute_reprojection_loss(self, pred, target):
        """Computes reprojection loss between a batch of predicted and target images
        """
        abs_diff = torch.abs(target - pred)
        l1_loss = abs_diff.mean(1, True)

        if self.opt.no_ssim:
            reprojection_loss = l1_loss
        else:
            ssim_loss = self.ssim(pred, target).mean(1, True)
            reprojection_loss = 0.85 * ssim_loss + 0.15 * l1_loss

        return reprojection_loss

    def compute_disp_losses(self, inputs, outputs, outputs_others):
        disp_losses = {}
        for scale in self.opt.scales:
            for frame_id in self.opt.frame_ids:
                if frame_id == 0:
                    disp = outputs[("disp", scale)]
                else:
                    disp = outputs_others[frame_id][("disp", scale)]
                depth_gt = inputs[("depth_gt_scale", frame_id, scale)]
                disp_gt = depth_to_disp(depth_gt, self.opt.min_depth, self.opt.max_depth)
                # print("disp_gt range", torch.min(disp_gt), torch.max(disp_gt))
                if frame_id == self.opt.frame_ids[0]:
                    disp_losses[scale] = torch.tensor(0, dtype=torch.float32, device=self.device)
                disp_losses[scale] += self.compute_disp_loss(disp, disp_gt)
        return disp_losses

    def compute_disp_loss(self, disp, disp_gt):
        mask = disp_gt > 0
        disp_gt_masked = disp_gt[mask] # becomes a 1-D tensor
        disp_masked = disp[mask]

        disp_error = torch.mean(torch.abs(disp_masked - disp_gt_masked))
        return disp_error


    def compute_losses(self, inputs, outputs, outputs_others):
        """Compute the reprojection and smoothness losses for a minibatch
        """
        losses = {}
        total_loss = 0

        ### ZMH: CVO loss
        # total_cvo_loss = 0
        # total_cos_loss = 0

        if self.opt.cvo_loss_dense:
            innerps = self.gen_innerp_dense(inputs, outputs)

        if not self.train_flag or self.opt.sup_cvo_pose_lidar:
            if self.opt.cvo_loss_dense:
                losses["loss_pose/cos_sum"] =  torch.tensor(0, dtype=torch.float32, device=self.device)
                losses["loss_pose/cvo_sum"] =  torch.tensor(0, dtype=torch.float32, device=self.device)
                for frame_id in self.opt.frame_ids[1:]:
                    for scale in self.opt.scales:
                        inp, f_dist, cos_sim = self.gen_cvo_loss_dense(innerps, (0, frame_id), scale, (True, True) )
                        losses["loss_pose/cvo_s{}_f{}".format(scale, frame_id)] = f_dist
                        losses["loss_pose/cos_s{}_f{}".format(scale, frame_id)] = cos_sim
                        losses["loss_pose/inp_s{}_f{}".format(scale, frame_id)] = inp

                        losses["loss_pose/cos_sum"] += cos_sim
                        losses["loss_pose/cvo_sum"] += f_dist
            else:
                self.calc_cvo_pose_loss(inputs, outputs, losses)

            

        disp_losses = self.compute_disp_losses(inputs, outputs, outputs_others)
        for scale in self.opt.scales:
            losses["loss_disp/{}".format(scale)] = disp_losses[scale]
        
        losses["loss_cvo/sum"] =  torch.tensor(0, dtype=torch.float32, device=self.device)
        losses["loss_cos/sum"] =  torch.tensor(0, dtype=torch.float32, device=self.device)
        losses["loss_inp/sum"] =  torch.tensor(0, dtype=torch.float32, device=self.device)

        for scale in self.opt.scales:
            loss = 0
            reprojection_losses = []

            if self.opt.v1_multiscale:
                source_scale = scale
            else:
                source_scale = 0

            disp = outputs[("disp", scale)]
            color = inputs[("color", 0, scale)]
            target = inputs[("color", 0, source_scale)]

            for frame_id in self.opt.frame_ids[1:]:
                pred = outputs[("color", frame_id, scale)]
                reprojection_losses.append(self.compute_reprojection_loss(pred, target))

            reprojection_losses = torch.cat(reprojection_losses, 1)

            ### ZMH: CVO loss
            
            if not self.train_flag or self.opt.supervised_by_gt_depth:
                if self.opt.cvo_loss:
                    # cvo_losses = []
                    # cos_losses = []
                    # cvo_loss = torch.tensor(0., dtype = reprojection_losses.dtype, device=reprojection_losses.device)
                    # cos_loss = torch.tensor(0., dtype = reprojection_losses.dtype, device=reprojection_losses.device)

                    # if True: #outputs[("xyz_in_host", 0, scale)][:,0:3,:].shape[2] <= 10000:
                    # gt_mode = self.opt.supervised_by_gt_depth:
                    # for gt_mode in [True, False]:
                    # if outputs[("xyz_in_host", 0, scale)][:,0:3,:].shape[2] <= 5000:
                    ### calculate cvo_loss only when the image scale is not too large
                    
                    # for frame_id in self.opt.frame_ids[1:]:
                    #     if gt_mode:
                    #         xyz_0 = outputs[("xyz_gt", frame_id, 0)]
                    #         rgb_0 = outputs[("rgb_gt", frame_id, 0)]
                    #     else:
                    #         xyz_0 = outputs[("xyz_in_host", 0, scale)]
                    #         rgb_0 = outputs[("rgb_in_host", 0, scale)]

                    #     xyz_1 = outputs[("xyz_in_host", frame_id, scale)]
                    #     rgb_1 = outputs[("rgb_in_host", frame_id, scale)]
                    
                    if self.opt.cvo_loss_dense:
                        for frame_id in self.opt.frame_ids[1:]:
                            inp, f_dist, cos_sim = self.gen_cvo_loss_dense(innerps, (0, frame_id), scale, (True, False) )
                            
                            losses["loss_cvo/{}_s{}_f{}".format(True, scale, frame_id)] = f_dist
                            losses["loss_cos/{}_s{}_f{}".format(True, scale, frame_id)] = cos_sim
                            losses["loss_inp/{}_s{}_f{}".format(True, scale, frame_id)] = inp
                            
                            losses["loss_cvo/sum"] += f_dist
                            losses["loss_cos/sum"] += cos_sim
                            losses["loss_inp/sum"] += inp

                    else:
                        for gt_mode in [True]:# [True, False]:
                            for frame_id in self.opt.frame_ids:
                                cvo_losses = {}
                                cos_losses = {}
                                innerp_losses = {}
                                # if frame_id == 1:
                                #     continue
                                if not gt_mode:
                                    if frame_id==self.opt.frame_ids[0]:
                                        xyz_0 = outputs[("xyz_gt", 0, scale)]
                                        rgb_0 = outputs[("rgb_gt", 0, scale)]
                                    else:
                                        xyz_0 = outputs[("xyz_in_host", frame_id, scale)]
                                        rgb_0 = outputs[("rgb_in_host", frame_id, scale)]

                                    xyz_1 = outputs[("xyz_in_host", 0, scale)]
                                    rgb_1 = outputs[("rgb_in_host", 0, scale)]
                                else:
                                    xyz_0 = outputs[("xyz_in_host", frame_id, scale)]
                                    rgb_0 = outputs[("rgb_in_host", frame_id, scale)]
                                    xyz_1 = outputs[("xyz_gt", 0, scale)]
                                    rgb_1 = outputs[("rgb_gt", 0, scale)]

                                for ib in range(self.opt.batch_size):
                                    vector_to_cvo = {}
                                    vector_to_cvo["xyz"] = {}
                                    vector_to_cvo["rgb"] = {}
                                    samp_pt = 3500 #4000  # the original number of points are about 5k, 1k, 0.3k, 0.1k (gen_pcl_gt masked out points without gt measurements)
                                    # print("pcl 0", "ib", ib, "frame_id", frame_id, "scale", scale, "size", xyz_0[ib].shape[-1])
                                    # print("pcl 1", "ib", ib, "frame_id", frame_id, "scale", scale, "size", xyz_1[ib].shape[-1])
                                    if xyz_0[ib].shape[-1] > samp_pt:
                                        # print('gt sampling!', scale)
                                        num_from_gt = xyz_0[ib].shape[-1]
                                        idx_gt = torch.randperm(num_from_gt)[:samp_pt]
                                        vector_to_cvo["xyz"][0] = xyz_0[ib][:,0:3,idx_gt] # self.opt.scales[-1]
                                        vector_to_cvo["rgb"][0] = rgb_0[ib][...,idx_gt]
                                    else:
                                        vector_to_cvo["xyz"][0] = xyz_0[ib][:,0:3,:]
                                        vector_to_cvo["rgb"][0] = rgb_0[ib]
                                    
                                    if xyz_1[ib].shape[-1] > samp_pt:
                                        # print('est sampling!', scale)
                                        num_from_est = xyz_1[ib].shape[-1]
                                        idx_est = torch.randperm(num_from_est)[:samp_pt]
                                        vector_to_cvo["xyz"][1] = xyz_1[ib][:,0:3,idx_est]
                                        vector_to_cvo["rgb"][1] = rgb_1[ib][...,idx_est]
                                        # vector_to_cvo["xyz"][1] = xyz_1[ib][:,0:3,idx_gt]
                                        # vector_to_cvo["rgb"][1] = rgb_1[ib][...,idx_gt]
                                    else:
                                        vector_to_cvo["xyz"][1] = xyz_1[ib][:,0:3,:]
                                        vector_to_cvo["rgb"][1] = rgb_1[ib]

                                    # if self.show_range and scale == 0 and frame_id == 1:
                                    #     print("xyz gt 0 min",  torch.min(vector_to_cvo["xyz"][0], dim=2)[0]) # x: [-70,70], y: [-3, 15], z: [-0.3, 80]
                                    #     print("xyz gt 0 max",  torch.max(vector_to_cvo["xyz"][0], dim=2)[0]) # x: [-26, 13], y: [-3, 2], z: [5, 76]
                                    #     print("xyz gt 1 min",  torch.min(vector_to_cvo["xyz"][1], dim=2)[0]) # 
                                    #     print("xyz gt 1 max",  torch.max(vector_to_cvo["xyz"][1], dim=2)[0])
                                    
                                    cvo_loss, cos_loss, innerp_loss = self.compute_cvo_loss( vector_to_cvo )

                                    for item in cvo_loss:
                                        if ib == 0:
                                            cvo_losses[item] = torch.tensor(0, dtype=torch.float32, device=self.device)
                                        cvo_losses[item] += cvo_loss[item] / ( self.opt.batch_size )
                                    for item in cos_loss:
                                        if ib == 0 :
                                            cos_losses[item] = torch.tensor(0, dtype=torch.float32, device=self.device)
                                        cos_losses[item] += cos_loss[item] / (self.opt.batch_size )
                                    for item in innerp_loss:
                                        if ib == 0:
                                            innerp_losses[item] = torch.tensor(0, dtype=torch.float32, device=self.device)
                                        innerp_losses[item] += innerp_loss[item] / ( self.opt.batch_size )

                                    # for item in cvo_loss:
                                    #     if ib == 0 and frame_id == self.opt.frame_ids[0]:
                                    #         cvo_losses[item] = torch.tensor(0, dtype=torch.float32, device=self.device)
                                    #     cvo_losses[item] += cvo_loss[item] / ( (len(self.opt.frame_ids)-1) * self.opt.batch_size )
                                    # for item in cos_loss:
                                    #     if ib == 0 and frame_id == self.opt.frame_ids[0]:
                                    #         cos_losses[item] = torch.tensor(0, dtype=torch.float32, device=self.device)
                                    #     cos_losses[item] += cos_loss[item] / ((len(self.opt.frame_ids)-1) * self.opt.batch_size )
                                    # for item in innerp_loss:
                                    #     if ib == 0 and frame_id == self.opt.frame_ids[0]:
                                    #         innerp_losses[item] = torch.tensor(0, dtype=torch.float32, device=self.device)
                                    #     innerp_losses[item] += innerp_loss[item] / ((len(self.opt.frame_ids)-1) * self.opt.batch_size )
                            
                                for item in cvo_loss:
                                    losses["loss_cvo/{}_{}_s{}_f{}".format(item, gt_mode, scale, frame_id)] = cvo_losses[item]
                                    losses["loss_cos/{}_{}_s{}_f{}".format(item, gt_mode, scale, frame_id)] = cos_losses[item]
                                    losses["loss_inp/{}_{}_s{}_f{}".format(item, gt_mode, scale, frame_id)] = innerp_losses[item]

                                    if gt_mode :#and scale >=2:
                                        losses["loss_cvo/sum"] += cvo_losses[item]
                                        losses["loss_cos/sum"] += cos_losses[item]
                                        losses["loss_inp/sum"] += innerp_losses[item]



            if not self.opt.disable_automasking:
                identity_reprojection_losses = []
                for frame_id in self.opt.frame_ids[1:]:
                    pred = inputs[("color", frame_id, source_scale)]
                    identity_reprojection_losses.append(
                        self.compute_reprojection_loss(pred, target))

                identity_reprojection_losses = torch.cat(identity_reprojection_losses, 1)

                if self.opt.avg_reprojection:
                    identity_reprojection_loss = identity_reprojection_losses.mean(1, keepdim=True)
                else:
                    # save both images, and do min all at once below
                    identity_reprojection_loss = identity_reprojection_losses

            elif self.opt.predictive_mask:
                # use the predicted mask
                mask = outputs["predictive_mask"]["disp", scale]
                if not self.opt.v1_multiscale:
                    mask = F.interpolate(
                        mask, [self.opt.height, self.opt.width],
                        mode="bilinear", align_corners=False)

                reprojection_losses *= mask

                # add a loss pushing mask to 1 (using nn.BCELoss for stability)
                weighting_loss = 0.2 * nn.BCELoss()(mask, torch.ones(mask.shape).cuda(self.device))
                loss += weighting_loss.mean()

            if self.opt.avg_reprojection:
                reprojection_loss = reprojection_losses.mean(1, keepdim=True)
            else:
                reprojection_loss = reprojection_losses

            if not self.opt.disable_automasking:
                # add random numbers to break ties
                identity_reprojection_loss += torch.randn(
                    identity_reprojection_loss.shape).cuda(self.device) * 0.00001

                combined = torch.cat((identity_reprojection_loss, reprojection_loss), dim=1)
            else:
                combined = reprojection_loss

            if combined.shape[1] == 1:
                to_optimise = combined
            else:
                to_optimise, idxs = torch.min(combined, dim=1)

            if not self.opt.disable_automasking:
                outputs["identity_selection/{}".format(scale)] = (
                    idxs > identity_reprojection_loss.shape[1] - 1).float()

            loss += to_optimise.mean()

            mean_disp = disp.mean(2, True).mean(3, True)
            norm_disp = disp / (mean_disp + 1e-7)
            smooth_loss = get_smooth_loss(norm_disp, color)

            loss += self.opt.disparity_smoothness * smooth_loss / (2 ** scale)
            total_loss += loss
            losses["loss/{}".format(scale)] = loss
            
            # if self.opt.cvo_loss:
            #     total_cvo_loss += cvo_loss
            #     losses["loss_cvo/{}".format(scale)] = cvo_loss
            #     total_cos_loss += cos_loss
            #     losses["loss_cos/{}".format(scale)] = cos_loss

        total_loss /= self.num_scales
        losses["loss"] = total_loss

        # if self.opt.cvo_loss:
        #     for item in cvo_loss:
        #         losses["loss_cvo/{}".format(item)] = cvo_losses[item]
        #         losses["loss_cos/{}".format(item)] = cos_losses[item]

        return losses

    def calc_cvo_pose_loss(self, inputs, outputs, losses):
        """
        This is to supervise the pose prediction by calculating the CVO loss between two lidar point clouds.
        """

        losses["loss_pose/cos_sum"] =  torch.tensor(0, dtype=torch.float32, device=self.device)
        losses["loss_pose/cvo_sum"] =  torch.tensor(0, dtype=torch.float32, device=self.device)
        # losses["loss_pose/inp_sum"] =  torch.tensor(0, dtype=torch.float32, device=self.device)
        for frame_id in self.opt.frame_ids[1:]:
            for scale in self.opt.scales:

                cvo_losses = {}
                cos_losses = {}
                innerp_losses = {}

                for ib in range(self.opt.batch_size):
                    if frame_id == "s":
                        T = inputs["stereo_T"]
                    else:
                        T = outputs[("cam_T_cam", 0, frame_id)][ib:ib+1] #T_i0
                        
                    vector_to_cvo = {}
                    vector_to_cvo["xyz"] = {}
                    # vector_to_cvo["xyz"][0] = torch.matmul(T, inputs[("velo_gt", 0)][ib].transpose(1,2))[:,:3,:]
                    # vector_to_cvo["xyz"][1] = inputs[("velo_gt", frame_id)][ib].transpose(1,2)[:,:3,:]
                    vector_to_cvo["xyz"][0] = torch.matmul(T, outputs[("xyz_gt", 0, scale)][ib])[:,:3,:]
                    vector_to_cvo["xyz"][1] = outputs[("xyz_gt", frame_id, scale)][ib][:,:3,:]

                    vector_to_cvo["rgb"] = {}
                    vector_to_cvo["rgb"][0] = outputs[("rgb_gt", 0, scale)][ib]
                    vector_to_cvo["rgb"][1] = outputs[("rgb_gt", frame_id, scale)][ib]

                    # print("xyz gt 0 min",  torch.min(vector_to_cvo["xyz"][0], dim=2)) # x: [-70,70], y: [-3, 15], z: [-0.3, 80]
                    # print("xyz gt 0 max",  torch.max(vector_to_cvo["xyz"][0], dim=2))
                    # print("xyz gt 1 min",  torch.min(vector_to_cvo["xyz"][1], dim=2))
                    # print("xyz gt 1 max",  torch.max(vector_to_cvo["xyz"][1], dim=2))
                    
                    # print("# of points: 0", vector_to_cvo["xyz"][0].shape)
                    # print("# of points: 1", vector_to_cvo["xyz"][1].shape)
                    ## ZMH: typically before sampling there are about 60k~65k points, a 640*192 image is about twice of that. 
                    
                    samp_num = 5000
                    for k in range(2):
                        num_el = vector_to_cvo["xyz"][k].shape[2]
                        if num_el > samp_num:
                            perm = torch.randperm( num_el )
                            idx = perm[:samp_num]
                            vector_to_cvo["xyz"][k] = vector_to_cvo["xyz"][k][:,:,idx]
                            vector_to_cvo["rgb"][k] = vector_to_cvo["rgb"][k][:,:,idx]
                    
                    # print("xyz pose 0 min",  torch.min(vector_to_cvo["xyz"][0], dim=2)[0]) # x: [-70,70], y: [-3, 15], z: [-0.3, 80]
                    # print("xyz pose 0 max",  torch.max(vector_to_cvo["xyz"][0], dim=2)[0])
                    # print("xyz pose 1 min",  torch.min(vector_to_cvo["xyz"][1], dim=2)[0])
                    # print("xyz pose 1 max",  torch.max(vector_to_cvo["xyz"][1], dim=2)[0])
                    # draw3DPts( vector_to_cvo["xyz"][0].detach(),  vector_to_cvo["xyz"][1].detach() )
                    # print("from pose")
                    cvo_loss, cos_loss, innerp_loss = self.compute_cvo_loss( vector_to_cvo )

                    for item in cvo_loss:
                        if ib == 0:
                            cvo_losses[item] = torch.tensor(0, dtype=torch.float32, device=self.device)
                        cvo_losses[item] += cvo_loss[item] / ((len(self.opt.frame_ids)-1) * self.opt.batch_size )
                    for item in cos_loss:
                        if ib == 0:
                            cos_losses[item] = torch.tensor(0, dtype=torch.float32, device=self.device)
                        cos_losses[item] += cos_loss[item] / ((len(self.opt.frame_ids)-1) * self.opt.batch_size )
                    for item in innerp_loss:
                        if ib == 0:
                            innerp_losses[item] = torch.tensor(0, dtype=torch.float32, device=self.device)
                        innerp_losses[item] += innerp_loss[item] / ((len(self.opt.frame_ids)-1) * self.opt.batch_size )

                    
                    for item in cvo_loss:
                        losses["loss_pose/cvo_{}_s{}_f{}".format(item, scale, frame_id)] = cvo_losses[item]
                        losses["loss_pose/cos_{}_s{}_f{}".format(item, scale, frame_id)] = cos_losses[item]
                        losses["loss_pose/inp_{}_s{}_f{}".format(item, scale, frame_id)] = innerp_losses[item]

                        losses["loss_pose/cvo_sum"] += cvo_losses[item]
                        losses["loss_pose/cos_sum"] += cos_losses[item]
                        # losses["loss_pose/inp_sum"] += innerp_losses[item]
                    

        # return cvo_losses, cos_losses, innerp_losses
            

    def compute_depth_losses(self, inputs, outputs, losses):
        """Compute depth metrics, to allow monitoring during training

        This isn't particularly accurate as it averages over the entire batch,
        so is only used to give an indication of validation performance
        """
        depth_pred = outputs[("depth", 0, 0)]

        if "TUM" not in self.opt.dataset:
            depth_pred = torch.clamp(F.interpolate(
                depth_pred, [375, 1242], mode="bilinear", align_corners=False), 1e-3, 80)
        else:
            depth_pred = torch.clamp(F.interpolate(
                depth_pred, [480, 640], mode="bilinear", align_corners=False), 1e-3, 80)

        depth_pred = depth_pred.detach()

        depth_gt = inputs["depth_gt"]
        mask = depth_gt > 0

        # garg/eigen crop
        if "TUM" not in self.opt.dataset:
            crop_mask = torch.zeros_like(mask)
            crop_mask[:, :, 153:371, 44:1197] = 1
            mask = mask * crop_mask

        depth_gt = depth_gt[mask]
        depth_pred = depth_pred[mask]
        depth_pred *= torch.median(depth_gt) / torch.median(depth_pred)

        depth_pred = torch.clamp(depth_pred, min=1e-3, max=80)

        depth_errors = compute_depth_errors(depth_gt, depth_pred)

        for i, metric in enumerate(self.depth_metric_names):
            losses[metric] = np.array(depth_errors[i].cpu())

    def log_time(self, batch_idx, duration, loss):
        """Print a logging statement to the terminal
        """
        samples_per_sec = self.opt.batch_size / duration
        time_sofar = time.time() - self.start_time
        training_time_left = (
            self.num_total_steps / self.step - 1.0) * time_sofar if self.step > 0 else 0
        print_string = "epoch {:>3} | batch {:>6} | examples/s: {:5.1f}" + \
            " | loss: {:.5f} | time elapsed: {} | time left: {}"
        print(print_string.format(self.epoch, batch_idx, samples_per_sec, loss,
                                  sec_to_hm_str(time_sofar), sec_to_hm_str(training_time_left)))

    def log(self, mode, inputs, outputs, losses):
        """Write an event to the tensorboard events file
        """
        writer = self.writers[mode]
        for l, v in losses.items():
            writer.add_scalar("{}".format(l), v, self.step)

        for j in range(min(4, self.opt.batch_size)):  # write a maxmimum of four images
            for s in self.opt.scales:
                for frame_id in self.opt.frame_ids:
                    if frame_id == 0: ## added by ZMH
                        writer.add_image(
                            "color_{}_{}/{}".format(frame_id, s, j),
                            inputs[("color", frame_id, s)][j].data, self.step)
                        if s == 0 and frame_id != 0:
                            writer.add_image(
                                "color_pred_{}_{}/{}".format(frame_id, s, j),
                                outputs[("color", frame_id, s)][j].data, self.step)

                writer.add_image(
                    "disp_{}/{}".format(s, j),
                    normalize_image(outputs[("disp", s)][j]), self.step)

                # if s == 0:
                    # print('shape 1', outputs[("disp", s)][j].shape)
                disp = depth_to_disp(inputs[("depth_gt_scale", 0, s)], self.opt.min_depth, self.opt.max_depth)
                disp = disp.squeeze(1)
                # print('shape 2', disp.shape)
                writer.add_image(
                    "disp_{}/gt_{}".format(s, j),
                    normalize_image(disp), self.step)

                writer.add_image(
                    "disp_{}/mask_{}".format(s, j),
                    inputs[("depth_mask", 0, s)][j], self.step)
                

                if self.opt.predictive_mask:
                    for f_idx, frame_id in enumerate(self.opt.frame_ids[1:]):
                        writer.add_image(
                            "predictive_mask_{}_{}/{}".format(frame_id, s, j),
                            outputs["predictive_mask"][("disp", s)][j, f_idx][None, ...],
                            self.step)

                elif not self.opt.disable_automasking:
                    if s == 0: ## added by ZMH
                        writer.add_image(
                            "automask_{}/{}".format(s, j),
                            outputs["identity_selection/{}".format(s)][j][None, ...], self.step)

    def save_opts(self):
        """Save options to disk so we know what we ran this experiment with
        """
        models_dir = os.path.join(self.log_path, "models" + "_"+self.ctime)
        if not os.path.exists(models_dir):
            os.makedirs(models_dir)
        to_save = self.opt.__dict__.copy()

        with open(os.path.join(models_dir, 'opt.json'), 'w') as f:
            json.dump(to_save, f, indent=2)

    def save_model(self):
        """Save model weights to disk
        """
        save_folder = os.path.join(self.log_path, "models" + "_"+self.ctime, "weights_{}".format(self.epoch))
        if not os.path.exists(save_folder):
            os.makedirs(save_folder)

        for model_name, model in self.models.items():
            save_path = os.path.join(save_folder, "{}.pth".format(model_name))
            to_save = model.state_dict()
            if model_name == 'encoder':
                # save the sizes - these are needed at prediction time
                to_save['height'] = self.opt.height
                to_save['width'] = self.opt.width
                to_save['use_stereo'] = self.opt.use_stereo
            torch.save(to_save, save_path)

        save_path = os.path.join(save_folder, "{}.pth".format("adam"))
        torch.save(self.model_optimizer.state_dict(), save_path)

    def load_model(self):
        """Load model(s) from disk
        """
        self.opt.load_weights_folder = os.path.expanduser(self.opt.load_weights_folder)

        assert os.path.isdir(self.opt.load_weights_folder), \
            "Cannot find folder {}".format(self.opt.load_weights_folder)
        print("loading model from folder {}".format(self.opt.load_weights_folder))

        for n in self.opt.models_to_load:
            print("Loading {} weights...".format(n))
            path = os.path.join(self.opt.load_weights_folder, "{}.pth".format(n))
            model_dict = self.models[n].state_dict()
            pretrained_dict = torch.load(path)
            pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict}
            model_dict.update(pretrained_dict)
            self.models[n].load_state_dict(model_dict)

        # loading adam state
        optimizer_load_path = os.path.join(self.opt.load_weights_folder, "adam.pth")
        if os.path.isfile(optimizer_load_path):
            print("Loading Adam weights")
            optimizer_dict = torch.load(optimizer_load_path)
            self.model_optimizer.load_state_dict(optimizer_dict)
        else:
            print("Cannot find Adam weights so Adam is randomly initialized")
