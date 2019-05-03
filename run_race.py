"""BERT finetuning runner."""

import logging
import os
import argparse
import random
from tqdm import tqdm, trange
import csv
import glob
import json
import apex

import numpy as np
import torch
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler

from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.modeling import BertForRace
from pytorch_pretrained_bert.optimization import BertAdam
from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE
from pytorch_pretrained_bert.dataset_utils import read_race_examples
from pytorch_pretrained_bert.dataset_utils import convert_examples_to_features

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


def accuracy(out, labels):
    outputs = np.argmax(out, axis=1)
    return np.sum(outputs == labels)


def select_article_field(features, field):
    article_list = []
    for feature in features:
        article_list.append(
            feature.article_features[0].choices_features[0][field])
    return article_list


def select_question_field(features, field):
    question_list = []
    for feature in features:
        question_list.append(
            feature.question_features[0].choices_features[0][field])
    return question_list


def select_answer_field(features, field):
    answer_list = []
    for feature in features:
        sub_list = []
        for answer in feature.answer_features:
            for item in answer.choices_features:
                sub_list.append(item[field])
        answer_list.append(sub_list)
    return answer_list


def warmup_linear(x, warmup=0.002):
    if x < warmup:
        return x / warmup
    return 1.0 - x


def main():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument("--data_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The input data dir. Should contain the .csv files (or other data files) for the task.")
    parser.add_argument("--bert_model", default=None, type=str, required=True,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                             "bert-large-uncased, bert-base-cased, bert-base-multilingual, bert-base-chinese.")
    parser.add_argument("--output_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The output directory where the model checkpoints will be written.")

    # Other parameters
    parser.add_argument("--max_article_length",
                        default=400,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--max_question_length",
                        default=30,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--max_answer_length",
                        default=16,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--do_train",
                        default=False,
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval",
                        default=False,
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_lower_case",
                        default=False,
                        action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--train_batch_size",
                        default=32,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size",
                        default=8,
                        type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--learning_rate",
                        default=5e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs",
                        default=3.0,
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument("--no_cuda",
                        default=False,
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument('--fp16',
                        default=False,
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale',
                        type=float, default=0,
                        help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
                             "0 (default value): dynamic loss scaling.\n"
                             "Positive power of 2: static loss scaling value.\n")

    args = parser.parse_args()
    device = torch.device(
        "cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    n_gpu = torch.cuda.device_count()

    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, bool(args.local_rank != -1), args.fp16))
    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
            args.gradient_accumulation_steps))

    args.train_batch_size = int(
        args.train_batch_size / args.gradient_accumulation_steps)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_eval:
        raise ValueError(
            "At least one of `do_train` or `do_eval` must be True.")

    if os.path.exists(args.output_dir) and os.listdir(args.output_dir):
        raise ValueError(
            "Output directory ({}) already exists and is not empty.".format(args.output_dir))
    os.makedirs(args.output_dir, exist_ok=True)

    tokenizer = BertTokenizer.from_pretrained(
        args.bert_model, do_lower_case=args.do_lower_case)

    train_examples = None
    num_train_steps = None
    if args.do_train:
        train_dir = os.path.join(args.data_dir, 'train')
        train_examples = read_race_examples(
            [train_dir + '/high', train_dir + '/middle'])
        num_train_steps = int(
            len(train_examples) / args.train_batch_size / args.gradient_accumulation_steps * args.num_train_epochs)

    # Prepare model
    model = BertForRace(args.bert_model,
                        cache_dir=PYTORCH_PRETRAINED_BERT_CACHE
                        / 'distributed_{}'.format(args.local_rank)
                        )
    if args.fp16:
        model.half()
    model.to(device)

    if n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Prepare optimizer
    param_optimizer = list(model.named_parameters())

    # hack to remove pooler, which is not used
    # thus it produce None grad that break apex
    param_optimizer = [n for n in param_optimizer if 'pooler' not in n[0]]

    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(
            nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(
            nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    t_total = num_train_steps

    if args.fp16:
        try:
            from apex.optimizers import FP16_Optimizer
            from apex.optimizers import FusedAdam
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        optimizer = FusedAdam(optimizer_grouped_parameters,
                              lr=args.learning_rate,
                              bias_correction=False,
                              max_grad_norm=1.0)
        if args.loss_scale == 0:
            optimizer = FP16_Optimizer(optimizer, dynamic_loss_scale=True)
        else:
            optimizer = FP16_Optimizer(
                optimizer, static_loss_scale=args.loss_scale)
    else:
        optimizer = BertAdam(optimizer_grouped_parameters,
                             lr=args.learning_rate,
                             warmup=args.warmup_proportion,
                             t_total=t_total)
    global_step = 0
    if args.do_train:
        train_features = convert_examples_to_features(
            train_examples, tokenizer, args.max_article_length, args.max_question_length, args.max_answer_length, True)
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_steps)

        article_input_ids = torch.tensor(select_article_field(
            train_features, 'input_ids'), dtype=torch.long)
        article_input_mask = torch.tensor(select_article_field(
            train_features, 'input_mask'), dtype=torch.long)
        article_segment_ids = torch.tensor(select_article_field(
            train_features, 'segment_ids'), dtype=torch.long)

        question_input_ids = torch.tensor(select_question_field(
            train_features, 'input_ids'), dtype=torch.long)
        question_input_mask = torch.tensor(select_question_field(
            train_features, 'input_mask'), dtype=torch.long)
        question_segment_ids = torch.tensor(select_question_field(
            train_features, 'segment_ids'), dtype=torch.long)

        answer_input_ids = torch.tensor(select_answer_field(
            train_features, 'input_ids'), dtype=torch.long)
        answer_input_mask = torch.tensor(select_answer_field(
            train_features, 'input_mask'), dtype=torch.long)
        answer_segment_ids = torch.tensor(select_answer_field(
            train_features, 'segment_ids'), dtype=torch.long)

        all_label = torch.tensor(
            [f.label for f in train_features], dtype=torch.long
        )

        train_data = TensorDataset(
            article_input_ids,
            article_input_mask,
            article_segment_ids,
            question_input_ids,
            question_input_mask,
            question_segment_ids,
            answer_input_ids,
            answer_input_mask,
            answer_segment_ids,
            all_label
        )
        train_sampler = RandomSampler(train_data)
        train_dataloader = DataLoader(
            train_data, sampler=train_sampler, batch_size=args.train_batch_size
        )

        model.train()
        for ep in range(int(args.num_train_epochs)):
            tr_loss = 0
            nb_tr_examples, nb_tr_steps = 0, 0
            logger.info("Trianing Epoch: {}/{}".format(ep +
                                                       1, int(args.num_train_epochs)))
            for step, batch in enumerate(train_dataloader):
                batch = tuple(t.to(device) for t in batch)
                article_input_ids, article_input_mask, article_segment_ids, question_input_ids, question_input_mask, question_segment_ids, answer_input_ids, answer_input_mask, answer_segment_ids, label_ids = batch
                loss = model(article_input_ids, article_segment_ids, article_input_mask, question_input_ids, question_segment_ids,
                             question_input_mask, answer_input_ids, answer_segment_ids, answer_input_mask, label_ids)
                if n_gpu > 1:
                    loss = loss.mean()  # mean() to average on multi-gpu.
                if args.fp16 and args.loss_scale != 1.0:
                    # rescale loss for fp16 training
                    # see https://docs.nvidia.com/deeplearning/sdk/mixed-precision-training/index.html
                    loss = loss * args.loss_scale
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                tr_loss += loss.item()
                nb_tr_examples += article_input_ids.size(0)
                nb_tr_steps += 1

                if args.fp16:
                    optimizer.backward(loss)
                else:
                    loss.backward()
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    # modify learning rate with special warm up BERT uses
                    lr_this_step = args.learning_rate * \
                        warmup_linear(global_step / t_total,
                                      args.warmup_proportion)
                    for param_group in optimizer.param_groups:
                        param_group['lr'] = lr_this_step
                    optimizer.step()
                    optimizer.zero_grad()
                    global_step += 1

                if global_step % 100 == 0:
                    logger.info("Training loss: {}, global step: {}".format(
                        tr_loss / nb_tr_steps, global_step))
                # evaluate on dev set
                if global_step % 1000 == 0 and global_step >= 1000:
                    dev_dir = os.path.join(args.data_dir, 'dev')
                    dev_set = [dev_dir + '/high', dev_dir + '/middle']

                    eval_examples = read_race_examples(dev_set)
                    eval_features = convert_examples_to_features(
                        eval_examples, tokenizer, args.max_article_length, args.max_question_length, args.max_answer_length, True)
                    logger.info("***** Running evaluation: Dev *****")
                    logger.info("  Num examples = %d", len(eval_examples))
                    logger.info("  Batch size = %d", args.eval_batch_size)

                    article_input_ids = torch.tensor(select_article_field(
                        eval_features, 'input_ids'), dtype=torch.long)
                    article_input_mask = torch.tensor(select_article_field(
                        eval_features, 'input_mask'), dtype=torch.long)
                    article_segment_ids = torch.tensor(select_article_field(
                        eval_features, 'segment_ids'), dtype=torch.long)

                    question_input_ids = torch.tensor(select_question_field(
                        eval_features, 'input_ids'), dtype=torch.long)
                    question_input_mask = torch.tensor(select_question_field(
                        eval_features, 'input_mask'), dtype=torch.long)
                    question_segment_ids = torch.tensor(select_question_field(
                        eval_features, 'segment_ids'), dtype=torch.long)

                    answer_input_ids = torch.tensor(select_answer_field(
                        eval_features, 'input_ids'), dtype=torch.long)
                    answer_input_mask = torch.tensor(select_answer_field(
                        eval_features, 'input_mask'), dtype=torch.long)
                    answer_segment_ids = torch.tensor(select_answer_field(
                        eval_features, 'segment_ids'), dtype=torch.long)
                    all_label = torch.tensor(
                        [f.label for f in eval_features], dtype=torch.long
                    )

                    eval_data = TensorDataset(
                        article_input_ids,
                        article_input_mask,
                        article_segment_ids,
                        question_input_ids,
                        question_input_mask,
                        question_segment_ids,
                        answer_input_ids,
                        answer_input_mask,
                        answer_segment_ids,
                        all_label
                    )
                    eval_sampler = SequentialSampler(eval_data)
                    eval_dataloader = DataLoader(
                        eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size
                    )

                    model.eval()
                    eval_loss, eval_accuracy = 0, 0
                    nb_eval_steps, nb_eval_examples = 0, 0
                    for step, batch in enumerate(eval_dataloader):
                        batch = tuple(t.to(device) for t in batch)
                        article_input_ids, article_input_mask, article_segment_ids, question_input_ids, question_input_mask, question_segment_ids, answer_input_ids, answer_input_mask, answer_segment_ids, label_ids = batch
                        with torch.no_grad():
                            tmp_eval_loss = model(
                                article_input_ids, article_segment_ids, article_input_mask, question_input_ids, question_segment_ids,
                                question_input_mask, answer_input_ids, answer_segment_ids, answer_input_mask, label_ids)
                            logits = model(article_input_ids, article_segment_ids, article_input_mask, question_input_ids, question_segment_ids,
                                           question_input_mask, answer_input_ids, answer_segment_ids, answer_input_mask, None)

                        logits = logits.detach().cpu().numpy()
                        label_ids = label_ids.to('cpu').numpy()
                        tmp_eval_accuracy = accuracy(logits, label_ids)

                        eval_loss += tmp_eval_loss.mean().item()
                        eval_accuracy += tmp_eval_accuracy

                        nb_eval_examples += article_input_ids.size(0)
                        nb_eval_steps += 1

                    eval_loss = eval_loss / nb_eval_steps
                    eval_accuracy = eval_accuracy / nb_eval_examples

                    result = {'dev_eval_loss': eval_loss,
                              'dev_eval_accuracy': eval_accuracy,
                              'global_step': global_step,
                              'loss': tr_loss / nb_tr_steps}

                    output_eval_file = os.path.join(
                        args.output_dir, "dev_results.txt")
                    with open(output_eval_file, "a+") as writer:
                        logger.info("***** Dev results *****")
                        for key in sorted(result.keys()):
                            logger.info("  %s = %s", key, str(result[key]))
                            writer.write("%s = %s\n" % (key, str(result[key])))
                    model.train()

    model_to_save = model.module if hasattr(
        model, 'module') else model  # Only save the model it-self
    output_model_file = os.path.join(args.output_dir, "pytorch_model.bin")
    torch.save(model_to_save.state_dict(), output_model_file)

    # Load a trained model that you have fine-tuned
    # use this part if you want to load the trained model
    # model_state_dict = torch.load(output_model_file)
    # model = BertForMultipleChoice.from_pretrained(args.bert_model,
    #     state_dict=model_state_dict,
    #     num_choices=4)
    # model.to(device)
    if args.do_eval:
        test_dir = os.path.join(args.data_dir, 'test')
        test_high = [test_dir + '/high']
        test_middle = [test_dir + '/middle']

        # test high
        eval_examples = read_race_examples(test_high)
        eval_features = convert_examples_to_features(
            eval_examples, tokenizer, args.max_article_length, args.max_question_length, args.max_answer_length, True)
        logger.info("***** Running evaluation: test high *****")
        logger.info("  Num examples = %d", len(eval_examples))
        logger.info("  Batch size = %d", args.eval_batch_size)

        article_input_ids = torch.tensor(select_article_field(
            eval_features, 'input_ids'), dtype=torch.long)
        article_input_mask = torch.tensor(select_article_field(
            eval_features, 'input_mask'), dtype=torch.long)
        article_segment_ids = torch.tensor(select_article_field(
            eval_features, 'segment_ids'), dtype=torch.long)

        question_input_ids = torch.tensor(select_question_field(
            eval_features, 'input_ids'), dtype=torch.long)
        question_input_mask = torch.tensor(select_question_field(
            eval_features, 'input_mask'), dtype=torch.long)
        question_segment_ids = torch.tensor(select_question_field(
            eval_features, 'segment_ids'), dtype=torch.long)

        answer_input_ids = torch.tensor(select_answer_field(
            eval_features, 'input_ids'), dtype=torch.long)
        answer_input_mask = torch.tensor(select_answer_field(
            eval_features, 'input_mask'), dtype=torch.long)
        answer_segment_ids = torch.tensor(select_answer_field(
            eval_features, 'segment_ids'), dtype=torch.long)
        all_label = torch.tensor(
            [f.label for f in eval_features], dtype=torch.long
        )
        eval_data = TensorDataset(
            article_input_ids,
            article_input_mask,
            article_segment_ids,
            question_input_ids,
            question_input_mask,
            question_segment_ids,
            answer_input_ids,
            answer_input_mask,
            answer_segment_ids,
            all_label
        )
        eval_sampler = SequentialSampler(eval_data)
        eval_dataloader = DataLoader(
            eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size
        )
        model.eval()
        high_eval_loss, high_eval_accuracy = 0, 0
        high_nb_eval_steps, high_nb_eval_examples = 0, 0
        for step, batch in enumerate(eval_dataloader):
            batch = tuple(t.to(device) for t in batch)
            article_input_ids, article_input_mask, article_segment_ids, question_input_ids, question_input_mask, question_segment_ids, answer_input_ids, answer_input_mask, answer_segment_ids, label_ids = batch
            with torch.no_grad():
                tmp_eval_loss = model(
                    article_input_ids, article_segment_ids, article_input_mask, question_input_ids, question_segment_ids,
                    question_input_mask, answer_input_ids, answer_segment_ids, answer_input_mask, label_ids)
                logits = model(article_input_ids, article_segment_ids, article_input_mask, question_input_ids, question_segment_ids,
                               question_input_mask, answer_input_ids, answer_segment_ids, answer_input_mask, None)

            logits = logits.detach().cpu().numpy()
            label_ids = label_ids.to('cpu').numpy()
            tmp_eval_accuracy = accuracy(logits, label_ids)

            high_eval_loss += tmp_eval_loss.mean().item()
            high_eval_accuracy += tmp_eval_accuracy

            high_nb_eval_examples += article_input_ids.size(0)
            high_nb_eval_steps += 1

        eval_loss = high_eval_loss / high_nb_eval_steps
        eval_accuracy = high_eval_accuracy / high_nb_eval_examples

        result = {'high_eval_loss': eval_loss,
                  'high_eval_accuracy': eval_accuracy}

        output_eval_file = os.path.join(args.output_dir, "test_results.txt")
        with open(output_eval_file, "a+") as writer:
            logger.info("***** Eval results *****")
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))

        # test middle
        eval_examples = read_race_examples(test_middle)
        eval_features = convert_examples_to_features(
            eval_examples, tokenizer, args.max_article_length, args.max_question_length, args.max_answer_length, True)
        logger.info("***** Running evaluation: test middle *****")
        logger.info("  Num examples = %d", len(eval_examples))
        logger.info("  Batch size = %d", args.eval_batch_size)

        article_input_ids = torch.tensor(select_article_field(
            eval_features, 'input_ids'), dtype=torch.long)
        article_input_mask = torch.tensor(select_article_field(
            eval_features, 'input_mask'), dtype=torch.long)
        article_segment_ids = torch.tensor(select_article_field(
            eval_features, 'segment_ids'), dtype=torch.long)

        question_input_ids = torch.tensor(select_question_field(
            eval_features, 'input_ids'), dtype=torch.long)
        question_input_mask = torch.tensor(select_question_field(
            eval_features, 'input_mask'), dtype=torch.long)
        question_segment_ids = torch.tensor(select_question_field(
            eval_features, 'segment_ids'), dtype=torch.long)

        answer_input_ids = torch.tensor(select_answer_field(
            eval_features, 'input_ids'), dtype=torch.long)
        answer_input_mask = torch.tensor(select_answer_field(
            eval_features, 'input_mask'), dtype=torch.long)
        answer_segment_ids = torch.tensor(select_answer_field(
            eval_features, 'segment_ids'), dtype=torch.long)
        all_label = torch.tensor(
            [f.label for f in eval_features], dtype=torch.long
        )
        eval_data = TensorDataset(
            article_input_ids,
            article_input_mask,
            article_segment_ids,
            question_input_ids,
            question_input_mask,
            question_segment_ids,
            answer_input_ids,
            answer_input_mask,
            answer_segment_ids,
            all_label
        )
        eval_sampler = SequentialSampler(eval_data)
        eval_dataloader = DataLoader(
            eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size
        )
        model.eval()
        middle_eval_loss, middle_eval_accuracy = 0, 0
        middle_nb_eval_steps, middle_nb_eval_examples = 0, 0
        for step, batch in enumerate(eval_dataloader):
            batch = tuple(t.to(device) for t in batch)
            article_input_ids, article_input_mask, article_segment_ids, question_input_ids, question_input_mask, question_segment_ids, answer_input_ids, answer_input_mask, answer_segment_ids, label_ids = batch
            with torch.no_grad():
                tmp_eval_loss = model(
                    article_input_ids, article_segment_ids, article_input_mask, question_input_ids, question_segment_ids,
                    question_input_mask, answer_input_ids, answer_segment_ids, answer_input_mask, label_ids)
                logits = model(article_input_ids, article_segment_ids, article_input_mask, question_input_ids, question_segment_ids,
                               question_input_mask, answer_input_ids, answer_segment_ids, answer_input_mask, None)

            logits = logits.detach().cpu().numpy()
            label_ids = label_ids.to('cpu').numpy()
            tmp_eval_accuracy = accuracy(logits, label_ids)

            middle_eval_loss += tmp_eval_loss.mean().item()
            middle_eval_accuracy += tmp_eval_accuracy

            middle_nb_eval_examples += article_input_ids.size(0)
            middle_nb_eval_steps += 1

        eval_loss = middle_eval_loss / middle_nb_eval_steps
        eval_accuracy = middle_eval_accuracy / middle_nb_eval_examples

        result = {'middle_eval_loss': eval_loss,
                  'middle_eval_accuracy': eval_accuracy}

        output_eval_file = os.path.join(args.output_dir, "test_results.txt")
        with open(output_eval_file, "a+") as writer:
            logger.info("***** Eval results *****")
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))

        # all test
        eval_loss = (middle_eval_loss + high_eval_loss) / \
            (middle_nb_eval_steps + high_nb_eval_steps)
        eval_accuracy = (middle_eval_accuracy + high_eval_accuracy) / \
            (middle_nb_eval_examples + high_nb_eval_examples)

        result = {'overall_eval_loss': eval_loss,
                  'overall_eval_accuracy': eval_accuracy}

        with open(output_eval_file, "a+") as writer:
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))


if __name__ == "__main__":
    main()
