from __future__ import absolute_import, division, print_function
import os
import sys
pythonpath = os.path.abspath(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
print(pythonpath)
sys.path.insert(0, pythonpath)
import numpy as np
from PIL import Image
import cv2
import os.path as op
import json
import time
import torch
import torch.distributed as dist
from apex import amp
import numpy as np
from PIL import Image
import cv2
import os.path as op
import json
import time
import torch
# import torch.distributed as dist
# import deepspeed
# from src.utils.deepspeed import fp32_to_fp16

from SeeRee.config import (basic_check_arguments, shared_configs)
from SeeRee.video_transforms import Compose, Resize, Normalize, CenterCrop
from SeeRee.volume_transforms import ClipToTensor
from SeeRee.caption_tensorizer import build_tensorizer
from SeeRee.utils.logger import LOGGER as logger
from SeeRee.utils.logger import (TB_LOGGER, RunningMeter, add_log_to_file)
from SeeRee.utils.comm import (is_main_process,
                            get_rank, get_world_size, dist_init)
from SeeRee.utils.miscellaneous import (mkdir, set_seed, str_to_bool)

from SeeRee.modeling.video_captioning_e2e_vid_swin_bert import VideoTransformer
from SeeRee.modeling.load_swin import get_swin_model, reload_pretrained_swin
from SeeRee.modeling.load_bert import get_bert_model


from numpysocket import NumpySocket
import time

port_num = int(int(os.getenv('MASTER_PORT'))/10) + 7000 # 9980



def _transforms(args, frames):
    raw_video_crop_list = [
        # Resize(args.img_res),
        # CenterCrop((args.img_res,args.img_res)),
        ClipToTensor(channel_nb=3),
        Normalize(mean=[0.42990619, 0.4654383 , 0.49828411],std=[0.21958288, 0.20122495, 0.1933832])
    ]            
    raw_video_prcoess = Compose(raw_video_crop_list)

    frames = frames.numpy()
    frames = np.transpose(frames, (0, 2, 3, 1))
    num_of_frames, height, width, channels = frames.shape

    frame_list = []
    for i in range(args.max_num_frames):
        frame_list.append(Image.fromarray(frames[i]))

    # apply normalization, output tensor (C x T x H x W) in the range [0, 1.0]
    crop_frames = raw_video_prcoess(frame_list)
    # (C x T x H x W) --> (T x C x H x W)
    crop_frames = crop_frames.permute(1, 0, 2, 3)
    return crop_frames 

def load_frames_from_files():
    split = 'val_seen'
    self_txt = json.load(open('/root/mount/Matterport3DSimulator/SwinBERT/datasets/commander_TATC/CVDN/NDH_commander/%s/%s.json'\
                        %(split,split), 'r'))
    self_img_path = '/root/mount/Matterport3DSimulator/SwinBERT/datasets/commander_TATC/CVDN/NDH_commander/%s/imgs'\
                        %split
    idx = 10


    while self_txt[idx]['len_images'] == 0:
        idx = (idx - 1) if idx - 1 >=0 else (idx + 1)
    item = self_txt[idx]
    
    # img = []
    # for b in self_img[item['video']]:
    #     img.append(self_str2img(b).unsqueeze(0))
    # img = T.cat(img, dim=0)
    
    img = []
    
    for i in range(item['len_images']):
        img.append(
            cv2.resize(cv2.imread(os.path.join(self_img_path, item['scan'] \
                + '_' + item['start_pano'] + '_'\
                + str(item['inst_idx']) + '_' + '%02d'%i +".jpg"), 1), (224,224))
        )
    for i in range(item['len_images'], args.max_num_frames):
        img.append( np.zeros_like(img[0]) )
    img = torch.from_numpy(np.stack(img).transpose(0, 3, 1, 2))
    '''
    ans_txt = item['dialog_answer']
    tok, mask = self_str2txt(item['dialog_question']+ans_txt) 
    # yue: finetune masking
    idxs = [i for i in range(len(mask))]
    np.random.shuffle(idxs)
    mask_pos = idxs[:int(0.15*len(mask))]

    masked_tok = []
    
    for i in range(len(tok)):
        if i in mask_pos:
            masked_tok.append(tok[i])
            tok[i] = 103
        else:
            masked_tok.append(-1)
    '''
   
    
    return img #, tok, mask, masked_tok

def inference(args, frames, model, tokenizer, tensorizer, question_txt = ''):
    cls_token_id, sep_token_id, pad_token_id, mask_token_id, period_token_id = \
        tokenizer.convert_tokens_to_ids([tokenizer.cls_token, tokenizer.sep_token,
        tokenizer.pad_token, tokenizer.mask_token, '.'])

    model.float()
    model.eval()
    # frames = _online_video_decode(args, video_path)
    # YUE
    # frames = load_frames_from_files() # torch.uint8
    preproc_frames = _transforms(args, frames) # torch.float32
    # data_sample = tensorizer.tensorize_example_e2e(question_txt, preproc_frames) 
    data_sample = tensorizer.tensorize_example_e2e(question_txt, preproc_frames, '', got_a_generate_b = args.got_a_generate_b, qa_as_caption = False, text_meta=None) # caption tensorizer

    data_sample = tuple(t.to(args.device) for t in data_sample)
    with torch.no_grad():

        inputs = {'is_decode': True,
            'input_ids': data_sample[0][None,:], 'attention_mask': data_sample[1][None,:],
            'token_type_ids': data_sample[2][None,:], 'img_feats': data_sample[3][None,:],
            'masked_pos': data_sample[4][None,:],
            'do_sample': False,
            'bos_token_id': cls_token_id,
            'pad_token_id': pad_token_id,
            'eos_token_ids': [sep_token_id],
            'mask_token_id': mask_token_id,
            # # for adding od labels
            # 'add_od_labels': args.add_od_labels, 'od_labels_start_posid': args.max_seq_a_length,
            # # hyperparameters of beam search
            # 'max_length': args.max_gen_length,
            'add_od_labels': args.add_od_labels, 'od_labels_start_posid': args.max_seq_a_length,
            # hyperparameters of beam search
            'max_length': args.max_seq_length if args.got_a_generate_b else args.max_gen_length,
            'num_beams': args.num_beams,
            "temperature": args.temperature,
            "top_k": args.top_k,
            "top_p": args.top_p,
            "repetition_penalty": args.repetition_penalty,
            "length_penalty": args.length_penalty,
            "num_return_sequences": args.num_return_sequences,
            "num_keep_best": args.num_keep_best,
        }
        tic = time.time()
        outputs = model(**inputs)

        time_meter = time.time() - tic
        all_caps = outputs[0]  # batch_size * num_keep_best * max_len
        all_confs = torch.exp(outputs[1])

        for caps, confs in zip(all_caps, all_confs):
            for cap, conf in zip(caps, confs):
                cap = tokenizer.decode(cap.tolist(), skip_special_tokens=True)
                logger.info(f"Prediction: {cap}")
                logger.info(f"Conf: {conf.item()}")

    logger.info(f"Inference model computing time: {time_meter} seconds")
    return cap

def check_arguments(args):
    # shared basic checks
    basic_check_arguments(args)
    # additional sanity check:
    args.max_img_seq_length = int((args.max_num_frames/2)*(int(args.img_res)/32)*(int(args.img_res)/32))
    
    if args.freeze_backbone or args.backbone_coef_lr == 0:
        args.backbone_coef_lr = 0
        args.freeze_backbone = True
    
    if 'reload_pretrained_swin' not in args.keys():
        args.reload_pretrained_swin = False

    if not len(args.pretrained_checkpoint) and args.reload_pretrained_swin:
        logger.info("No pretrained_checkpoint to be loaded, disable --reload_pretrained_swin")
        args.reload_pretrained_swin = False

    if args.learn_mask_enabled==True: 
        args.attn_mask_type = 'learn_vid_att'

def update_existing_config_for_inference(args):
    ''' load swinbert args for evaluation and inference 
    '''
    assert args.do_test or args.do_eval
    checkpoint = args.eval_model_dir
    try:
        json_path = op.join(checkpoint, os.pardir, 'log', 'args.json')
        f = open(json_path,'r')
        json_data = json.load(f)

        from easydict import EasyDict
        train_args = EasyDict(json_data)
    except Exception as e:
        train_args = torch.load(op.join(checkpoint, 'training_args.bin'))

    train_args.eval_model_dir = args.eval_model_dir
    train_args.resume_checkpoint = args.eval_model_dir + 'model.bin'
    train_args.model_name_or_path = './SwinBERT/models/captioning/bert-base-uncased/'
    train_args.do_train = False
    train_args.do_eval = True
    train_args.do_test = True
    train_args.test_video_fname = args.test_video_fname
    return train_args

def get_custom_args(base_config):
    parser = base_config.parser
    parser.add_argument('--max_num_frames', type=int, default=32)
    parser.add_argument('--img_res', type=int, default=224)
    parser.add_argument('--patch_size', type=int, default=32)
    parser.add_argument("--grid_feat", type=str_to_bool, nargs='?', const=True, default=True)
    parser.add_argument("--kinetics", type=str, default='400', help="400 or 600")
    parser.add_argument("--pretrained_2d", type=str_to_bool, nargs='?', const=True, default=False)
    parser.add_argument("--vidswin_size", type=str, default='base')
    parser.add_argument('--freeze_backbone', type=str_to_bool, nargs='?', const=True, default=False)
    parser.add_argument('--use_checkpoint', type=str_to_bool, nargs='?', const=True, default=False)
    parser.add_argument('--backbone_coef_lr', type=float, default=0.001)
    parser.add_argument("--reload_pretrained_swin", type=str_to_bool, nargs='?', const=True, default=False)
    parser.add_argument('--learn_mask_enabled', type=str_to_bool, nargs='?', const=True, default=False)
    parser.add_argument('--loss_sparse_w', type=float, default=0)
    parser.add_argument('--sparse_mask_soft2hard', type=str_to_bool, nargs='?', const=True, default=False)
    parser.add_argument('--transfer_method', type=int, default=-1,
                        help="0: load all SwinBERT pre-trained weights, 1: load only pre-trained sparse mask")
    parser.add_argument('--att_mask_expansion', type=int, default=-1,
                        help="-1: random init, 0: random init and then diag-based copy, 1: interpolation")
    parser.add_argument('--resume_checkpoint', type=str, default='None')
    parser.add_argument('--test_video_fname', type=str, default='None')
    parser.add_argument('--no_cos_mask', '-no_cos_mask', action='store_true', default=False)


    args = base_config.parse_args()
    return args

def main(args):
    args = update_existing_config_for_inference(args)
    # global training_saver
    args.device = torch.device(args.device)
    # Setup CUDA, GPU & distributed training
    dist_init(args)
    check_arguments(args)
    set_seed(args.seed, args.num_gpus)
    fp16_trainning = None
    logger.info(
        "device: {}, n_gpu: {}, rank: {}, "
        "16-bits training: {}".format(
            args.device, args.num_gpus, get_rank(), fp16_trainning))

    if not is_main_process():
        logger.disabled = True

    logger.info(f"Pytorch version is: {torch.__version__}")
    logger.info(f"Cuda version is: {torch.version.cuda}")
    logger.info(f"cuDNN version is : {torch.backends.cudnn.version()}" )

     # Get Video Swin model 
    swin_model = get_swin_model(args)
    # Get BERT and tokenizer 
    bert_model, config, tokenizer = get_bert_model(args)
    # build SwinBERT based on training configs
    vl_transformer = VideoTransformer(args, config, swin_model, bert_model) 
    vl_transformer.freeze_backbone(freeze=args.freeze_backbone)

    # load weights for inference
    logger.info(f"Loading state dict from checkpoint {args.resume_checkpoint}")
    cpu_device = torch.device('cpu')
    pretrained_model = torch.load(args.resume_checkpoint, map_location=cpu_device)

    if isinstance(pretrained_model, dict):
        vl_transformer.load_state_dict(pretrained_model, strict=False)
    else:
        vl_transformer.load_state_dict(pretrained_model.state_dict(), strict=False)

    vl_transformer.to(args.device)
    vl_transformer.eval()

    tensorizer = build_tensorizer(args, tokenizer, is_train=False)

  
    # take the server name and port name
    
    # host = 'local host'
    # port = 5000

    # s_sending = socket.socket(socket.AF_INET,
    #                 socket.SOCK_STREAM)
    # s_sending.connect(('127.0.0.1', port))
    while True:
        with NumpySocket() as s:
            s.bind(('', port_num)) #9980 9996
            while True:
                    s.listen()
                    conn, addr = s.accept()
                    while conn:
                        cap = conn.recv(bufsize=16)
                        if len(cap) == 0:
                            break
                        if len(cap.shape)==2:
                            break
                    if len(cap.shape)==2:
                        break
        with NumpySocket() as s:
            time.sleep(0.1)
            s.connect(("localhost", port_num+1)) # 9981 9997
            s.sendall(np.array([[str(cap[0,0])]*100]))
            
        with NumpySocket() as s:
            s.bind(('', port_num+3)) # 9983 9999

            while True:
                    s.listen()
                    conn, addr = s.accept()

                    while conn:
                        frame = conn.recv()
                        if len(frame) == 0:
                            break
                        if len(frame.shape)==4:
                            break
                    if len(frame.shape)==4: # check it is valid input
                        break
            if np.sum(frame)<1:
                continue
        frames = [i for i in frame]
        for i in range(len(frames), args.max_num_frames):
            frames.append( np.zeros_like(frames[0]) )
        frames = torch.from_numpy(np.stack(frames).transpose(0, 3, 1, 2))
        cap = inference(args, frames, vl_transformer, tokenizer, tensorizer, question_txt = str(cap[0,0]))
        with NumpySocket() as s:
            time.sleep(0.1)
            s.connect(("localhost", port_num +2)) # 9982 9998
            s.sendall(np.array([[cap]*100]))
    # inference(args, frames, vl_transformer, tokenizer, tensorizer)


if __name__ == "__main__":
    shared_configs.shared_video_captioning_config(cbs=True, scst=True)
    args = get_custom_args(shared_configs)
    main(args)



