import sys
import argparse
import os
import csv
import shutil
import random
import yaml
import numpy as np
import torch
import math
from tqdm import tqdm
from torch import nn, optim
from torch.utils.data import DataLoader
from entropy_models import EntropyBottleneck, estimate_bpp
from networks import __CODER_TYPES__, AugmentedNormalizedFlowHyperPriorCoder
from torchvision.utils import save_image
from pathlib import Path
from dataloader import VideoTestData, VideoTestSequence, BitstreamData, BitstreamSequence
from flownets import PWCNet, SPyNet
from SDCNet import MotionExtrapolationNet
from models import Refinement
from util.psnr import mse2psnr
from util.sampler import Resampler
from util.ssim import MS_SSIM
from util.tools import Alignment, BitStreamIO
import torch.nn.functional as F

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
#=======================================================================================================================
class CompressModel(nn.Module):#nn.Module
    """Basic Compress Model"""

    def __init__(self):
        super(CompressModel, self).__init__()

    def named_main_parameters(self, prefix=''):
        for name, param in self.named_parameters(prefix=prefix, recurse=True):
            if 'quantiles' not in name:
                yield (name, param)

    def main_parameters(self):
        for _, param in self.named_main_parameters():
            yield param

    def named_aux_parameters(self, prefix=''):
        for name, param in self.named_parameters(prefix=prefix, recurse=True):
            if 'quantiles' in name:
                yield (name, param)

    def aux_parameters(self):
        for _, param in self.named_aux_parameters():
            yield param

    def aux_loss(self):
        aux_loss = []
        for m in self.modules():
            if isinstance(m, EntropyBottleneck):
                aux_loss.append(m.aux_loss())

        return torch.stack(aux_loss).mean()

#=======================================================================================================================
class Pframe(CompressModel):
    def __init__(self, args, mo_coder, cond_mo_coder, res_coder):
        super(Pframe, self).__init__()
        self.args = args
        self.criterion = nn.MSELoss(reduction='none').to(DEVICE) if not self.args.msssim else MS_SSIM(data_range=1.).to(DEVICE)
        
        self.if_model = AugmentedNormalizedFlowHyperPriorCoder(128, 320, 192, num_layers=2, use_QE=True, use_affine=False,
                                                              use_context=True, condition='GaussianMixtureModel', quant_mode='round').to(DEVICE) \
                                                              if self.args.Iframe == 'ANFIC' else None
        if self.args.MENet == 'PWC':
            self.MENet = PWCNet(trainable=True).to(DEVICE)
        elif self.args.MENet == 'SPy':
            self.MENet = SPyNet(trainable=False).to(DEVICE)

        self.MWNet = MotionExtrapolationNet(sequence_length=3).to(DEVICE)
        self.MWNet.__delattr__('flownet')

        self.Motion = mo_coder.to(DEVICE)
        self.CondMotion = cond_mo_coder.to(DEVICE)

        self.Resampler = Resampler().to(DEVICE)
        self.MCNet = Refinement(6, 64, out_channels=3).to(DEVICE)

        self.Residual = res_coder.to(DEVICE)

        self.ModeCoder = nn.Sequential(nn.Conv2d(8,32,3,stride=1, padding=1), nn.LeakyReLU(0.1), nn.Conv2d(32,2,3,stride=1, padding=1))

        self.frame_buffer = list()
        self.flow_buffer = list()
        self.align = Alignment().to(DEVICE)

    def load_args(self, args):
        self.args = args

    def motion_forward(self, ref_frame, coding_frame, p_order=1):
        # To generate extrapolated motion for conditional motion coding or not
        # "False" for first P frame (p_order == 1)
        predict = p_order > 1
        if predict:
            assert len(self.frame_buffer) == 3 or len(self.frame_buffer) == 2
            
            # Update frame buffer ; motion (flow) buffer will be updated in self.MWNet
            if len(self.frame_buffer) == 3:
                frame_buffer = [self.frame_buffer[0], self.frame_buffer[1], self.frame_buffer[2]]

            else:
                frame_buffer = [self.frame_buffer[0], self.frame_buffer[0], self.frame_buffer[1]]

            pred_frame, pred_flow = self.MWNet(frame_buffer, self.flow_buffer if len(self.flow_buffer) == 2 else None, True)

            flow = self.MENet(ref_frame, coding_frame)

            # Encode motion condioning on extrapolated motion
            flow_hat, likelihood_m, _, _ = self.CondMotion(flow, xc=pred_flow, x2_back=pred_flow, temporal_cond=pred_frame)

        # No motion extrapolation is performed for first P frame
        else: 
            flow = self.MENet(ref_frame, coding_frame)
            # Encode motion unconditionally
            flow_hat, likelihood_m = self.Motion(flow)


        warped_frame = self.Resampler(ref_frame, flow_hat)
        mc_frame = self.MCNet(ref_frame, warped_frame)

        self.MWNet.append_flow(flow_hat)

        return mc_frame, likelihood_m, flow_hat

    #code a frame using its reference frame
    def forward(self, ref_frame, coding_frame, p_order=1):
        mc_frame, likelihood_m, flow_hat = self.motion_forward(ref_frame, coding_frame, p_order)

        x = torch.cat((ref_frame, mc_frame, flow_hat),dim=1)
        mode_info = self.ModeCoder(x)
        alpha, beta = mode_info.chunk(2, dim=1)
        alpha = torch.sigmoid(alpha)
        beta = torch.sigmoid(beta)

        p = (1-beta)*mc_frame + beta*ref_frame
        c = (1-alpha)*p
        alpha_coding_frame = (1-alpha)*coding_frame
        reconstructed1, likelihood_r, x2, _ = self.Residual(alpha_coding_frame, xc=c, x2_back=c,
                                                          temporal_cond=c)
        reconstructed1 = reconstructed1.clamp(0, 1)
        reconstructed = reconstructed1 + alpha*p
        likelihoods = likelihood_m + likelihood_r

        return reconstructed, likelihoods, x2, mc_frame, alpha, beta

    @torch.no_grad()
    def test(self, action='test'):
        outputs = []
        for batch_idx, batch in tqdm(enumerate(self.test_loader)):
            if action == 'test':
                outputs.append(self.test_step(batch, batch_idx))
            elif action == 'compress':
                outputs.append(self.test_step(batch, batch_idx, TO_COMPRESS=True))
            elif action == 'decompress':
                outputs.append(self.decompress_step(batch, batch_idx))
        
        self.test_epoch_end(outputs)

    def test_step(self, batch, batch_idx, TO_COMPRESS=False):
        if self.args.msssim:
            similarity_metrics = 'MS-SSIM'
        else:
            similarity_metrics = 'PSNR'
        
        if TO_COMPRESS:
            metrics_name = [similarity_metrics, 'Rate']
        else:
            metrics_name = [similarity_metrics, 'Rate', 'Mo_Rate']
        metrics = {}
        for m in metrics_name:
            metrics[m] = []

        dataset_name, seq_name, batch, frame_id_start = batch

        ref_frame = batch[:, 0] # BPG-compressed I-frame in position 0
        batch = batch[:, 1:] # coding frames
        seq_name = seq_name[0]
        dataset_name = dataset_name[0]

        gop_size = batch.size(1)

        height, width = ref_frame.size()[2:]

        log_list = []

        # To align frame into multiplications of 64 ; zero-padding is performed
        align = Alignment().to(DEVICE)
        
        # Clear motion buffer & frame buffer
        self.MWNet.clear_buffer()
        self.frame_buffer = list()

        if TO_COMPRESS:
            file_pth = os.path.join(self.args.bitstream_dir, dataset_name, seq_name)
            os.makedirs(file_pth, exist_ok=True)
        for frame_idx in range(gop_size):

            ref_frame = ref_frame.clamp(0, 1)
            coding_frame = batch[:, frame_idx].to(DEVICE)

            # P-frame
            if frame_idx != 0:
                if TO_COMPRESS:
                    if frame_idx == 1:
                        self.frame_buffer = [align.align(ref_frame)]
                    
                    file_name = os.path.join(file_pth, f'{int(frame_id_start+frame_idx)}.bin')
                    rec_frame, streams, shapes = self.compress(align.align(ref_frame), align.align(coding_frame), frame_idx)
                    rec_frame = rec_frame.clamp(0, 1)
                    self.frame_buffer.append(rec_frame)
                    
                    with BitStreamIO(file_name, 'w') as fp:
                        fp.write(streams, [coding_frame.size()]+shapes)

                    rec_frame = align.resume(rec_frame)

                    # Read the binary files directly for accurate bpp estimate.
                    size_byte = os.path.getsize(file_name)
                    rate = size_byte * 8 / height / width
                    
                    mse = self.criterion(rec_frame, coding_frame).mean().item()

                    if self.args.msssim:
                        similarity = mse
                    else:
                        similarity = mse2psnr(mse)

                    metrics[similarity_metrics].append(similarity)
                    metrics['Rate'].append(rate)

                else:
                    if frame_idx == 1:
                        self.frame_buffer = [align.align(ref_frame)]
                    
                    rec_frame, likelihoods,_,_,_,_ = self(align.align(ref_frame), align.align(coding_frame), frame_idx)

                    rec_frame = rec_frame.clamp(0, 1)
                    self.frame_buffer.append(rec_frame)

                    # Back to original resolution
                    rec_frame = align.resume(rec_frame)
                    rate = estimate_bpp(likelihoods, input=coding_frame).mean().item()
                    
                    mse = self.criterion(rec_frame, coding_frame).mean().item()

                    if self.args.msssim:
                        similarity = mse
                    else:
                        similarity = mse2psnr(mse)

                    metrics[similarity_metrics].append(similarity)
                    metrics['Rate'].append(rate)

                    # likelihoods[0] & [1] are motion latent & hyper likelihood
                    m_rate = estimate_bpp(likelihoods[0], input=coding_frame).mean().item() + \
                             estimate_bpp(likelihoods[1], input=coding_frame).mean().item()
                    metrics['Mo_Rate'].append(m_rate)
                
                    log_list.append({similarity_metrics: similarity, 'Rate': rate, 'Mo_Rate': m_rate,
                                     'my': estimate_bpp(likelihoods[0], input=coding_frame).item(),
                                     'mz': estimate_bpp(likelihoods[1], input=coding_frame).item(),
                                     'ry': estimate_bpp(likelihoods[2], input=coding_frame).item(),
                                     'rz': estimate_bpp(likelihoods[3], input=coding_frame).item()})

                # Update frame buffer
                if len(self.frame_buffer) == 4:
                    self.frame_buffer.pop(0)
                    assert len(self.frame_buffer) == 3, str(len(self.frame_buffer))
            
            # I-frame
            else:
                if TO_COMPRESS and self.args.Iframe == 'ANFIC':
                    file_name = os.path.join(file_pth, f'{int(frame_id_start+frame_idx)}.bin')
                    rec_frame, streams, shapes = self.if_model.compress(align.align(coding_frame), return_hat=True)
                    
                    with BitStreamIO(file_name, 'w') as fp:
                        fp.write(streams, [coding_frame.size()]+shapes)

                    rec_frame = align.resume(rec_frame.to(DEVICE)).clamp(0, 1)
                    # Read the binary files directly for accurate bpp estimate.
                    size_byte = os.path.getsize(file_name)
                    rate = size_byte * 8 / height / width

                elif self.args.Iframe == 'ANFIC':
                    rec_frame, likelihoods, _ = self.if_model(align.align(coding_frame))
                    rec_frame = align.resume(rec_frame.to(DEVICE)).clamp(0, 1)
                    rate = estimate_bpp(likelihoods, input=rec_frame).mean().item()

                else:
                    rec_frame = ref_frame.to(DEVICE)
                    qp = {256: 37, 512: 32, 1024: 27, 2048: 22}[self.args.lmda]

                    # Read the binary files directly for accurate bpp estimate
                    # One should refer to `dataloader.py` to see the setting of BPG binary file path
                    size_byte = os.path.getsize(f'{self.args.dataset_path}/bpg/{qp}/bin/{seq_name}/frame_{int(frame_id_start+frame_idx)}.bin')
                    rate = size_byte * 8 / height / width

                if self.args.msssim:
                    similarity = self.criterion(rec_frame, coding_frame).mean().item()
                else:
                    mse = self.criterion(rec_frame, coding_frame).mean().item()
                    similarity = mse2psnr(mse)

                metrics[similarity_metrics].append(similarity)
                metrics['Rate'].append(rate)

                log_list.append({similarity_metrics: similarity, 'Rate': rate})

            # Make reconstruction as next reference frame
            ref_frame = rec_frame

        for m in metrics_name:
            metrics[m] = np.mean(metrics[m])

        logs = {'dataset_name': dataset_name, 'seq_name': seq_name, 'metrics': metrics, 'log_list': log_list,}
        return {'test_log': logs}

    def test_epoch_end(self, outputs):

        metrics_name = list(outputs[0]['test_log']['metrics'].keys())  # Get all metrics' names

        rd_dict = {}

        single_seq_logs = {}
        for metrics in metrics_name:
            single_seq_logs[metrics] = {}

        single_seq_logs['LOG'] = {}
        single_seq_logs['GOP'] = {}  # Will not be printed currently
        single_seq_logs['Seq_Names'] = []

        for logs in [log['test_log'] for log in outputs]:
            dataset_name = logs['dataset_name']
            seq_name = logs['seq_name']

            if not (dataset_name in rd_dict.keys()):
                rd_dict[dataset_name] = {}
                
                for metrics in metrics_name:
                    rd_dict[dataset_name][metrics] = []

            for metrics in logs['metrics'].keys():
                rd_dict[dataset_name][metrics].append(logs['metrics'][metrics])

            # Initialize
            if seq_name not in single_seq_logs['Seq_Names']:
                single_seq_logs['Seq_Names'].append(seq_name)
                for metrics in metrics_name:
                    single_seq_logs[metrics][seq_name] = []
                single_seq_logs['LOG'][seq_name] = []
                single_seq_logs['GOP'][seq_name] = []

            # Collect metrics logs
            for metrics in metrics_name:
                single_seq_logs[metrics][seq_name].append(logs['metrics'][metrics])
            single_seq_logs['LOG'][seq_name].extend(logs['log_list'])
            single_seq_logs['GOP'][seq_name] = len(logs['log_list'])

        os.makedirs(self.args.logs_dir + f'/report', exist_ok=True)

        for seq_name, log_list in single_seq_logs['LOG'].items():
            with open(self.args.logs_dir + f'/report/{seq_name}.csv', 'w', newline='') as report:
                writer = csv.writer(report, delimiter=',')
                columns = ['frame'] + list(log_list[1].keys())
                writer.writerow(columns)

                for idx in range(len(log_list)):
                    writer.writerow([f'frame_{idx + 1}'] + list(log_list[idx].values()))

        # Summary
        logs = {}
        print_log = '{:>16} '.format('Sequence_Name')
        for metrics in metrics_name:
            print_log += '{:>12}'.format(metrics)
        print_log += '\n'

        for seq_name in single_seq_logs['Seq_Names']:
            print_log += '{:>16} '.format(seq_name)

            for metrics in metrics_name:
                print_log += '{:12.4f}'.format(np.mean(single_seq_logs[metrics][seq_name]))

            print_log += '\n'
        print_log += '================================================\n'
        for dataset_name, rd in rd_dict.items():
            print_log += '{:>16} '.format(dataset_name)

            for metrics in metrics_name:
                logs['test/' + dataset_name + ' ' + metrics] = np.mean(rd[metrics])
                print_log += '{:12.4f}'.format(np.mean(rd[metrics]))

            print_log += '\n'

        print(print_log)

        os.makedirs(self.args.logs_dir, exist_ok=True)
        with open(self.args.logs_dir + f'/brief_summary.txt', 'w', newline='') as report:
            report.write(print_log)

    def decompress_step(self, batch, batch_idx):
        metrics_name = ['Rate']
        metrics = {}
        for m in metrics_name:
            metrics[m] = []

        dataset_name, seq_name, batch, frame_id_start = batch

        seq_name = seq_name[0]
        dataset_name = dataset_name[0]

        gop_size = len(batch)

        # To align frame into multiplications of 64 ; zero-padding is performed
        align = Alignment()

        log_list = []
        
        # Clear motion buffer & frame buffer
        self.MWNet.clear_buffer()
        self.frame_buffer = list()
        
        save_dir = self.args.logs_dir + f'/reconstructed/{seq_name}/'
        os.makedirs(save_dir, exist_ok=True)

        for frame_idx in range(gop_size):
            # P-frame
            if frame_idx != 0:
                ref_frame = ref_frame.clamp(0, 1)

                if frame_idx == 1:
                    self.frame_buffer = [align.align(ref_frame)]
                
                file_name = batch[frame_idx][0]

                with BitStreamIO(file_name, 'r') as fp:
                    stream_list, shape_list = fp.read_file()
                
                rec_frame = self.decompress(align.align(ref_frame), stream_list, shape_list[1:], frame_idx).to(DEVICE)
                rec_frame = rec_frame.clamp(0, 1)
                self.frame_buffer.append(rec_frame)
                rec_frame = align.resume(rec_frame, shape=shape_list[0])

                height, width = shape_list[0][2:]
                # Read the binary files directly for accurate bpp estimate.
                size_byte = os.path.getsize(file_name)
                rate = size_byte * 8 / height / width
                    
                metrics['Rate'].append(rate)

                log_list.append({'Rate': rate})

                # Update frame buffer
                if len(self.frame_buffer) == 4:
                    self.frame_buffer.pop(0)
                    assert len(self.frame_buffer) == 3, str(len(self.frame_buffer))
            
            # I-frame
            else:
                if self.args.Iframe == 'ANFIC':
                    file_name = batch[frame_idx][0]
                    with BitStreamIO(file_name, 'r') as fp:
                        stream_list, shape_list = fp.read_file()
                    
                    rec_frame = self.if_model.decompress(stream_list, shape_list[1:]).to(DEVICE)
                    rec_frame = align.resume(rec_frame, shape=shape_list[0]).clamp(0, 1)
                    
                    # Read the binary files directly for accurate bpp estimate.
                    height, width = shape_list[0][2:]
                    size_byte = os.path.getsize(file_name)
                    rate = size_byte * 8 / height / width
                else:
                    rec_frame = batch[frame_idx].to(DEVICE)
                    qp = {256: 37, 512: 32, 1024: 27, 2048: 22}[self.args.lmda]

                    # Read the binary files directly for accurate bpp estimate
                    # One should refer to `dataloader.py` to see the setting of BPG binary file path
                    height, width = rec_frame.size()[2:]
                    size_byte = os.path.getsize(f'{self.args.dataset_path}/bpg/{qp}/bin/{seq_name}/frame_{int(frame_id_start+frame_idx)}.bin')
                    rate = size_byte * 8 / height / width

                metrics['Rate'].append(rate)

                log_list.append({'Rate': rate})
            
            # Store reconstructed frame
            save_image(rec_frame[0], os.path.join(save_dir, f'frame_{int(frame_id_start + frame_idx)}.png'))

            # Make reconstructed frame as next reference frame
            ref_frame = rec_frame

        for m in metrics_name:
            metrics[m] = np.mean(metrics[m])

        logs = {'dataset_name': dataset_name, 'seq_name': seq_name, 'metrics': metrics, 'log_list': log_list,}

        return {'test_log': logs}

    def compress(self, ref_frame, coding_frame, p_order):
        # To generate extrapolated motion for conditional motion coding or not
        # "False" for first P frame (p_order == 1)
        predict = p_order > 1 
        if predict:
            assert len(self.frame_buffer) == 3 or len(self.frame_buffer) == 2
            
            # Update frame buffer ; motion (flow) buffer will be updated in self.MWNet
            if len(self.frame_buffer) == 3:
                frame_buffer = [self.frame_buffer[0], self.frame_buffer[1], self.frame_buffer[2]]

            else:
                frame_buffer = [self.frame_buffer[0], self.frame_buffer[0], self.frame_buffer[1]]

            pred_frame, pred_flow = self.MWNet(frame_buffer, self.flow_buffer if len(self.flow_buffer) == 2 else None, True)
            
            flow = self.MENet(ref_frame, coding_frame)
            
            # Encode motion condioning on extrapolated motion
            flow_hat, mv_strings, mv_shape = self.CondMotion.compress(flow, x2_back=pred_flow,xc=pred_flow, temporal_cond=pred_frame, 
                                                                      return_hat=True)

        # No motion extrapolation is performed for first P frame
        else: 
            flow = self.MENet(ref_frame, coding_frame)
            # Encode motion unconditionally
            flow_hat, mv_strings, mv_shape = self.Motion.compress(flow, return_hat=True)

        warped_frame = self.Resampler(ref_frame, flow_hat)
        mc_frame = self.MCNet(ref_frame, warped_frame)

        self.MWNet.append_flow(flow_hat)

        reconstructed, res_strings, res_shape = self.Residual.compress(coding_frame, x2_back=mc_frame, xc=mc_frame, temporal_cond=mc_frame, return_hat=True)

        strings, shapes = mv_strings + res_strings, mv_shape + res_shape

        return reconstructed, strings, shapes

    def decompress(self, ref_frame, strings, shapes, p_order):
        predict = p_order > 1 

        mv_strings, mv_shape = strings[:2], shapes[:2]

        if predict:
            assert len(self.frame_buffer) == 3 or len(self.frame_buffer) == 2
            
            # Update frame buffer ; motion (flow) buffer will be updated in self.MWNet
            if len(self.frame_buffer) == 3:
                frame_buffer = [self.frame_buffer[0], self.frame_buffer[1], self.frame_buffer[2]]

            else:
                frame_buffer = [self.frame_buffer[0], self.frame_buffer[0], self.frame_buffer[1]]

            pred_frame, pred_flow = self.MWNet(frame_buffer, self.flow_buffer if len(self.flow_buffer) == 2 else None, True)
            
            # Decode motion condioning on extrapolated motion
            flow_hat = self.CondMotion.decompress(mv_strings, mv_shape, 
                                                  x2_back=pred_flow,xc=pred_flow, temporal_cond=pred_frame)

        # No motion extrapolation is performed for first P frame
        else: 
            # Decode motion unconditionally
            flow_hat = self.Motion.decompress(mv_strings, mv_shape)

        warped_frame = self.Resampler(ref_frame, flow_hat)
        mc_frame = self.MCNet(ref_frame, warped_frame)

        self.MWNet.append_flow(flow_hat)

        res_strings, res_shape = strings[2:], shapes[2:]
        reconstructed = self.Residual.decompress(res_strings, res_shape,
                                                 x2_back=mc_frame, xc=mc_frame, temporal_cond=mc_frame)
        return reconstructed

    def setup(self):
        qp = {256: 37, 512: 32, 1024: 27, 2048: 22, 4096: 22}[self.args.lmda]
        
        if not (self.args.seq is None): # Test a single sequence
            if self.args.action == "test" or self.args.action == "compress":
                self.test_dataset = VideoTestSequence(self.args.dataset_path, self.args.lmda,
                                                      self.args.dataset, self.args.seq, self.args.seq_len, self.args.GOP)
            else:
                self.test_dataset = BitstreamSequence(self.args.dataset_path, self.args.lmda,
                                                      self.args.dataset, self.args.seq, self.args.seq_len, self.args.GOP,
                                                      self.args.bitstream_dir, self.args.Iframe=='BPG'
                                                     )
        else: # Test whole dataset
            if self.args.action == "test" or self.args.action == "compress":
                self.test_dataset = VideoTestData(self.args.dataset_path, self.args.lmda, (self.args.dataset), self.args.GOP)
            else:
                self.test_dataset = BitstreamData(self.args.dataset_path, self.args.lmda, (self.args.dataset), self.args.GOP,
                                                  self.args.bitstream_dir, self.args.Iframe=='BPG'
                                                 )

        self.test_loader = DataLoader(self.test_dataset, batch_size=1, num_workers=4, shuffle=False)

        if self.args.action == "compress" or self.args.action == "decompress":
            if self.args.Iframe == 'ANFIC':
                self.if_model.conditional_bottleneck.to("cpu")
            self.Motion.conditional_bottleneck.to("cpu")
            self.CondMotion.conditional_bottleneck.to("cpu")
            self.Residual.conditional_bottleneck.to("cpu")
#=======================================================================================================================
if __name__ == '__main__':
    # sets seeds for numpy, torch, etc...
    # must do for DDP to work well
    seed = 888888
    os.environ["PL_GLOBAL_SEED"] = str(seed)
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True

    save_root = os.getenv('LOG', './') + 'torchDVC/'

    parser = argparse.ArgumentParser(add_help=True)

    # Model architecture specification
    parser.add_argument('--Iframe', type=str, choices=['BPG', 'ANFIC'], default='BPG')
    parser.add_argument('--MENet', type=str, choices=['PWC', 'SPy'], default='PWC')
    parser.add_argument('--motion_coder_conf', type=str, default='./config/DVC_motion.yml')
    parser.add_argument('--cond_motion_coder_conf', type=str, default='./config/CANF_motion_predprior.yml')
    parser.add_argument('--residual_coder_conf', type=str, default='./config/CANF_inter_coder.yml')

    # Dataset configuration
    parser.add_argument('--dataset', type=str, choices=['U', 'B', 'C', 'D', 'E', 'M'], default=None)
    parser.add_argument('--seq', type=str, default=None, help='Specify a sequence to be encoded')
    parser.add_argument('--seq_len', type=int, default=100, help='The length of specified sequence')
    parser.add_argument('--dataset_path', default='./video_dataset', type=str)
    parser.add_argument('--bitstream_dir', default='./bin', type=str,
                        help='Path to store binary files generated when `compress` and used when `decompress`')

    # Testing specific
    parser.add_argument('--lmda', default=2048, type=int)
    parser.add_argument('--batch_size', default=16, type=int)
    parser.add_argument('--msssim', action="store_true")
    parser.add_argument('--GOP', type=int, default=32)

    # Others
    parser.add_argument('--model_dir', default='./weights', type=str)
    parser.add_argument('--logs_dir', default='./logs', type=str)
    parser.set_defaults(gpus=1)

    parser.add_argument(
        '--action', type=str, choices=['test', 'compress', 'decompress', 'train'],
        help="What to do: \n"
             "'test' takes video frames (in .png format) and perform compression simulation.\n"
             "'compress' takes video frames (in .png format) and writes compressed binary file for each frame.\n"
             "'decompress' reads binary files and reconstructs the whole video frame by frame (in PNG format).\n"
    )

    # parse params
    args = parser.parse_args()
    #===================================================================================================================
    #Testing.....
    #===================================================================================================================
    print('Testing LCCM-VC...')
    # Config codecs
    assert not (args.motion_coder_conf is None)
    mo_coder_cfg = yaml.safe_load(open(args.motion_coder_conf, 'r'))
    mo_coder_arch = __CODER_TYPES__[mo_coder_cfg['model_architecture']]
    mo_coder = mo_coder_arch(**mo_coder_cfg['model_params'])

    assert not (args.cond_motion_coder_conf is None)
    cond_mo_coder_cfg = yaml.safe_load(open(args.cond_motion_coder_conf, 'r'))
    cond_mo_coder_arch = __CODER_TYPES__[cond_mo_coder_cfg['model_architecture']]
    cond_mo_coder = cond_mo_coder_arch(**cond_mo_coder_cfg['model_params'])

    assert not (args.residual_coder_conf is None)
    res_coder_cfg = yaml.safe_load(open(args.residual_coder_conf, 'r'))
    res_coder_arch = __CODER_TYPES__[res_coder_cfg['model_architecture']]
    res_coder = res_coder_arch(**res_coder_cfg['model_params'])

    net = Pframe(args, mo_coder, cond_mo_coder, res_coder).cuda()

    model = torch.nn.DataParallel(net)

    checkpoint_net = torch.load(f"{args.model_dir}/{args.lmda}.ckpt",
                                map_location=(lambda storage, loc: storage))
    model.load_state_dict(checkpoint_net['state_dict'], strict=True)

    model = model.cuda()

    model.module.setup()

    model.eval()
    model.module.test(action=args.action)
