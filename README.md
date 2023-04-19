# A PyTorch implementation of LCCM-VC: Learned Conditional Coding Modes For Video Coding
## Original Paper:
H. Hadizadeh and I. V. Baji\'c, "LCCM-VC: Learned Conditional Coding Modes For Video Coding," in IEEE ICASSP Workshop on Humans, Machines and Multimedia - Quality of Experience and Beyond, Jun., 2023.

## Installation:
Note: LCCM-VC is built on CANF-VC [https://github.com/NYCU-MAPL/CANF-VC].
1. Clone CANF-VC from https://github.com/NYCU-MAPL/CANF-VC
2. Create a conda environment and install the requirements for CANF-VC as instructed at https://github.com/NYCU-MAPL/CANF-VC
3. Copy "eval_lccm_vc.py" from this repo to the main directory of CANF-VC. This is the main file for LCCM-VC.

## Checkpoints:
The checkpoints for LCCM-VC are available via the following Google Drive link:

https://drive.google.com/drive/folders/1fYlJJCC9EoSr2zm5zHVsnfRt3qK6zrNq?usp=sharing

## Datasets
To evaluate CANF-VC on a video dataset, follow the procedure mentioned for CANF-VC [https://github.com/NYCU-MAPL/CANF-VC] to prepare the datasets. 

## Evaluation:
The testing procedure for LCCM-VC is the same as CANF-VC. Please follow the evaluation commands provided at https://github.com/NYCU-MAPL/CANF-VC. However, instead of "test.py" you need to use "eval_lccm_vc.py". 

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

