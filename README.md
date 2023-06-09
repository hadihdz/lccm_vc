# A PyTorch implementation of LCCM-VC: Learned Conditional Coding Modes For Video Coding
## Original Paper:
H. Hadizadeh and I. V. Baji\'c, "LCCM-VC: Learned Conditional Coding Modes For Video Coding," in IEEE ICASSP Workshop on Humans, Machines and Multimedia - Quality of Experience and Beyond, June, 2023. [[arXiv](https://arxiv.org/abs/2210.15883)]

## Installation:
Note: LCCM-VC is built on CANF-VC [https://github.com/NYCU-MAPL/CANF-VC].
1. Clone CANF-VC from https://github.com/NYCU-MAPL/CANF-VC
2. Create a conda environment and install the requirements for CANF-VC as instructed at https://github.com/NYCU-MAPL/CANF-VC
3. Copy "eval_lccm_vc.py" from this repo to the main directory of CANF-VC. This is the main file for LCCM-VC.

## Checkpoints:
The checkpoints for LCCM-VC are available via the following Google Drive link:

https://drive.google.com/drive/folders/1fYlJJCC9EoSr2zm5zHVsnfRt3qK6zrNq?usp=sharing

Please download them, and put them in a folder like "weights" in the main directory of CANF-VC. There are 4 checkpoints corresponding to 4 lambda values {256,512,1024,2048}. Similar to CANF-VC, using input arguments like "--mode_dir ./weights --lmda 2048", you can load the checkpoint via "eval_lccm_vc.py". 

## Datasets
To evaluate CANF-VC on a video dataset, follow the procedure mentioned for CANF-VC [https://github.com/NYCU-MAPL/CANF-VC] to prepare the datasets. 

## Evaluation:
The testing procedure for LCCM-VC is the same as CANF-VC. Please follow the evaluation commands provided at https://github.com/NYCU-MAPL/CANF-VC. However, instead of "test.py" you need to use "eval_lccm_vc.py". For instance, for testing LCCM-VC, you can use this command:
```
$ python3 eval_lccm_vc.py --Iframe=ANFIC --MENet=PWC --motion_coder_conf=./CANF_VC/config/DVC_motion.yml --cond_motion_coder_conf=./CANF_VC/config/CANF_motion_predprior.yml --residual_coder_conf=./CANF_VC/config/CANF_inter_coder.yml --dataset=B --dataset_path=./video_dataset --lmda=2048 --model_dir=./weights --action=test --GOP=32
```
For compressing/decompressing, simply use --action=compress or --action=decompress in the above command.

## Citation:
If you find LCCM-VC useful for your research, please cite it as follows:

```
@InProceedings{lccm,
  author = "H. Hadizadeh and I. V. Baji\'c",
  title = "{LCCM-VC}: Learned Conditional Coding Modes for Video Coding",
  booktitle = "IEEE ICASSP Workshop on Humans, Machines and Multimedia - Quality of Experience and Beyond",
  month = "Jun.",
  year = "2023",
}
```

