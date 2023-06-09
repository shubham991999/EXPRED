from dataclasses import asdict

import sys

import argparse
import logging
import wandb as wandb
from typing import List, Dict, Set, Tuple

import torch
import os
import json
import numpy as np
import random

from itertools import chain

from expred import metrics
from expred.params import MTLParams
from expred.models.mlp_mtl import BertMTL, BertClassifier
from expred.tokenizer import BertTokenizerWithMapping
from expred.models.pipeline.mtl_pipeline_utils import decode
from expred.utils import load_datasets, load_documents, write_jsonl, Annotation
from expred.models.pipeline.mtl_token_identifier import train_mtl_token_identifier
from expred.models.pipeline.mtl_evidence_classifier import train_mtl_evidence_classifier
from expred.eraser_utils import get_docids

BATCH_FIRST = True
torch.cuda.empty_cache()



def initialize_models(conf: dict,
                      tokenizer: BertTokenizerWithMapping,
                      batch_first: bool) -> Tuple[BertMTL, BertClassifier, Dict[int, str]]:
    """
    Does several things:
    1. Create a mapping from label names to ids
    2. Configure and create the multi task learner, the first stage of the model (BertMTL)
    3. Configure and create the evidence classifier, second stage of the model (BertClassifier)
    :param conf:
    :param tokenizer:
    :param batch_first:
    :return: BertMTL, BertClassifier, label mapping
    """
    assert batch_first
    max_length = conf['max_length']
    # label mapping
    labels = dict((y, x) for (x, y) in enumerate(conf['classes']))
    

    # configure multi task learner
    mtl_params = MTLParams
    mtl_params.num_labels = len(labels)
    mtl_params.dim_exp_gru = conf['dim_exp_gru']
    mtl_params.dim_cls_linear = conf['dim_cls_linear']
    bert_dir = conf['bert_dir']
    use_half_precision = bool(conf['mtl_token_identifier'].get('use_half_precision', 1))
    evidence_identifier = BertMTL(bert_dir=bert_dir,
                                  tokenizer=tokenizer,
                                  mtl_params=mtl_params,
                                  max_length=max_length,
                                  use_half_precision=use_half_precision)

    #set up the evidence classifier
    use_half_precision  =  bool(conf['evidence_classifier'].get('use_half_precision', 1))
    print("Labels ",labels)
    print("use half precision",use_half_precision)
    evidence_classifier = BertClassifier(bert_dir=bert_dir,
                                         pad_token_id=tokenizer.pad_token_id,
                                         cls_token_id=tokenizer.cls_token_id,
                                         sep_token_id=tokenizer.sep_token_id,
                                         num_labels=mtl_params.num_labels,
                                         max_length=max_length,
                                         mtl_params=mtl_params,
                                         use_half_precision=use_half_precision)

    return evidence_identifier, evidence_classifier, labels


logging.basicConfig(level=logging.DEBUG, format='%(relativeCreated)6d %(threadName)s %(message)s')
logger = logging.getLogger(__name__)

# let's make this more or less deterministic (not resistent to restarts)
random.seed(12345)
np.random.seed(67890)
torch.manual_seed(10111213)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False


# or, uncomment the following sentences to make it more than random
# rand_seed_1 = ord(os.urandom(1)) * ord(os.urandom(1))
# rand_seed_2 = ord(os.urandom(1)) * ord(os.urandom(1))
# rand_seed_3 = ord(os.urandom(1)) * ord(os.urandom(1))
# random.seed(rand_seed_1)
# np.random.seed(rand_seed_2)
# torch.manual_seed(rand_seed_3)
# torch.backends.cudnn.deterministic = False
# torch.backends.cudnn.benchmark = True

import time
from time import gmtime, strftime

def main():
    # setup the Argument Parser
    parser = argparse.ArgumentParser(description=('Trains a pipeline model.\n'
                                                  '\n'
                                                  'Step 1 is evidence identification, the MTL happens here. It '
                                                  'predicts the label of the current sentence and tags its\n '
                                                  '        sub-tokens in the same time \n'
                                                  '    Step 2 is evidence classification, a BERT classifier takes the output of the evidence identifier and predicts its \n'
                                                  '        sentiment. Unlike in Deyong et al. this classifier takes in the same length as the identifier\'s input but with \n'
                                                  '        irrational sub-tokens masked.\n'
                                                  '\n'
                                                  '    These models should be separated into two separate steps, but at the moment:\n'
                                                  '    * prep data (load, intern documents, load json)\n'
                                                  '    * convert data for evidence identification - in the case of training data we take all the positives and sample some negatives\n'
                                                  '        * side note: this sampling is *somewhat* configurable and is done on a per-batch/epoch basis in order to gain a broader sampling of negative values.\n'
                                                  '    * train evidence identification\n'
                                                  '    * convert data for evidence classification - take all rationales + decisions and use this as input\n'
                                                  '    * train evidence classification\n'
                                                  '    * decode first the evidence, then run classification for each split\n'
                                                  '\n'
                                                  '    '), formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--data_dir', dest='data_dir', required=True,
                        help='Which directory contains a {train,val,test}.jsonl file?')
    parser.add_argument('--output_dir', dest='output_dir', required=True,
                        help='Where shall we write intermediate models + final data to?')
    parser.add_argument('--conf', dest='conf', required=True,
                        help='JSoN file for loading arbitrary model parameters (e.g. optimizers, pre-saved files, etc.')
    parser.add_argument('--batch_size', type=int, required=False, default=None,
                        help='Overrides the batch_size given in the config file. Helpful for debugging')
    args = parser.parse_args(args=['--data_dir', 'data/movies', '--output_dir', './trained_data','--conf','params/movies_expred.json'])
    print(args.data_dir)

    #wandb.init(project="expred")
    #wandb.init(project="expred")
    #Configure
    os.makedirs(args.output_dir, exist_ok=True)

    #loads the config
    
    with open(args.conf, 'r') as fp:
        logger.info(f'Loading configuration from {args.conf}')
        conf = json.load(fp)
        if args.batch_size is not None:
            logger.info(
                'Overwriting batch_sizes'
                f'(mtl_token_identifier:{conf["mtl_token_identifier"]["batch_size"]}'
                f'evidence_classifier:{conf["evidence_classifier"]["batch_size"]})'
                f'provided in config by command line argument({args.batch_size})'
            )
            conf['mtl_token_identifier']['batch_size'] = args.batch_size
            conf['evidence_classifier']['batch_size'] = args.batch_size
        logger.info(f'Configuration: {json.dumps(conf, indent=2, sort_keys=True)}')
    

    #todo add seeds
    #wandb.config.update(conf)
    #wandb.config.update(args)

    #load the annotation data

    #here train, val and test are the list of Annotation dataclass
    train, val, test = load_datasets(args.data_dir)
    #print("Trained Data----")
    #print(train[0])
    #print("Validation Data----")
    #print(val[0])
    #print("Test Data----")
    #print(test[0])

    #get's all docids needed that are contained in the loaded splits
    
    docids: Set[str] = set(chain.from_iterable(map(lambda ann: get_docids(ann),
                                               chain(train, val, test))))

    
    #print("Doc IDs-----")
    #print(docids)

    #All the documents get loaded. 
    documents: Dict[str, List[List[str]]] = load_documents(args.data_dir, docids)
    
    #print("Documents----")
    #print(documents.keys())
    
    
    #print(documents['negR_000.txt'])
    logger.info(f'Load {len(documents)} documents')
    #this ignores the case where annotations don't align perfectly with token boundaries, but this isn't that important
    
    
    
    tokenizer = BertTokenizerWithMapping.from_pretrained(conf['bert_vocab'])
    
    #logger.info(f'We have {len(word_interner)} wordpieces')

    #tokenizes and caches tokenized_docs, same for annotations
    #todo typo here? slides == slices (words?)
    #converts 
    tokenized_docs, tokenized_doc_token_slices = tokenizer.encode_docs(documents, args.output_dir)

    #print("Tokenized Docs----")
    #print(tokenized_docs.keys())
    #print(len(tokenized_docs['negR_000.txt']))
    #print("\nTokenized Docs Slides -\n")
    #print( tokenized_doc_token_slices['negR_000.txt'][7])
    
    indexed_train, indexed_val, indexed_test = [tokenizer.encode_annotations(data) for data in [train, val, test]]
    
    #print("Indexed Trained ------ ")
    #print(type(indexed_train))
    #print(indexed_train[0])


    
    mtl_token_identifier, evidence_classifier, labels_mapping =initialize_models(conf, tokenizer, batch_first=BATCH_FIRST)
    
    logger.info('Beginning training of the MTL identifier')
     
    
    from datetime import datetime
    start = datetime.now()
    print("INITIAL TIME for mtl token identifier classifier------",start)
    mtl_token_identifier = mtl_token_identifier.cuda()
    mtl_token_identifier, mtl_token_identifier_results, \
    train_machine_annotated, eval_machine_annotated, test_machine_annotated = \
        train_mtl_token_identifier(mtl_token_identifier,
                                   args.output_dir,
                                   indexed_train,
                                   indexed_val,
                                   indexed_test,
                                   labels_mapping=labels_mapping,
                                   interned_documents=tokenized_docs,
                                   source_documents=documents,
                                   token_mapping=tokenized_doc_token_slices,
                                   model_pars=conf,
                                   tensorize_model_inputs=True)
    

    #print("Train machine Annotated---")
    #print(train_machine_annotated)
    #print("MTL_Token Identifier Results---")
    #print(mtl_token_identifier_results)
    torch.cuda.empty_cache()
    mtl_token_identifier = mtl_token_identifier.cpu()
    end=datetime.now()
    print("END TIME for mtl token identifier classifier-----",end) 
    print("Time taken by mtl token identifier :",end-start)   
    #evidence identifier endstrain_mtl_evidence_classifier
    
    logger.info('Beginning training of the evidence classifier')
    evidence_classifier = evidence_classifier.cuda()
    optimizer = None
    scheduler = None

    #trains the classifier on the masked (based on rationales) documents
    
    start = datetime.now()
    print("START TIME FOR EVIDENCE CLASSIFIER :",start)
    evidence_classifier, evidence_class_results = train_mtl_evidence_classifier(evidence_classifier,
                                                                                args.output_dir,
                                                                                train_machine_annotated,
                                                                                eval_machine_annotated,
                                                                                tokenized_docs,
                                                                                conf,
                                                                                optimizer=optimizer,
                                                                                scheduler=scheduler,
                                                                                class_interner=labels_mapping,
                                                                                tensorize_model_inputs=True)
    #evidence classifier ends
   
    end = datetime.now()
    print("END TIME FOR EVIDENCE CLASSIFIER :",end)
    print("Time Taken by the evidence_classifier-----",end-start)

    

    logger.info('Beginning final decoding')
    mtl_token_identifier = mtl_token_identifier.cuda()
    
    pipeline_batch_size = min([conf['evidence_classifier']['batch_size'], conf['mtl_token_identifier']['batch_size']])
    pipeline_results, train_decoded, val_decoded, test_decoded = decode(evidence_identifier=mtl_token_identifier,
                                                                        evidence_classifier=evidence_classifier,
                                                                        train=indexed_train,
                                                                        mrs_train=train_machine_annotated,
                                                                        val=indexed_val,
                                                                        mrs_eval=eval_machine_annotated,
                                                                        test=indexed_test,
                                                                        mrs_test=test_machine_annotated,
                                                                        source_documents=documents,
                                                                        interned_documents=tokenized_docs,
                                                                        token_mapping=tokenized_doc_token_slices,
                                                                        class_interner=labels_mapping,
                                                                        tensorize_modelinputs=True,
                                                                        batch_size=pipeline_batch_size,
                                                                        tokenizer=tokenizer)

    
    write_jsonl(train_decoded, os.path.join(args.output_dir, 'train_decoded.jsonl'))
    write_jsonl(val_decoded, os.path.join(args.output_dir, 'val_decoded.jsonl'))
    write_jsonl(test_decoded, os.path.join(args.output_dir, 'test_decoded.jsonl'))
    with open(os.path.join(args.output_dir, 'identifier_results.json'), 'w') as ident_output, \
            open(os.path.join(args.output_dir, 'classifier_results.json'), 'w') as class_output:
        ident_output.write(json.dumps(mtl_token_identifier_results))
        class_output.write(json.dumps(evidence_class_results))
    for k, v in pipeline_results.items():
        if type(v) is dict:
            for k1, v1 in v.items():
                logging.info(f'Pipeline results for {k}, {k1}={v1}')
        else:
            logging.info(f'Pipeline results {k}\t={v}')
    
    
    #decode ends
    scores = metrics.main(
        [
            '--data_dir', args.data_dir,
            '--split', 'test',
            '--results', os.path.join(args.output_dir, 'test_decoded.jsonl'),
            '--score_file', os.path.join(args.output_dir, 'test_scores.jsonl')
        ]
    )

    #wandb.log(scores)

    #wandb.save(os.path.join(args.output_dir, '*.jsonl'))
    torch.cuda.empty_cache()
    
    
    
    



if __name__ == '__main__':
    main()
