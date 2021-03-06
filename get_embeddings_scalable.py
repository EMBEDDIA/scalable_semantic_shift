import torch
from transformers import BertTokenizer, BertModel
import numpy as np
import pickle
import gc
import re
from collections import defaultdict
import pandas as pd
import json
import nltk
from sklearn.metrics.pairwise import cosine_similarity
from nltk import RegexpTokenizer
import argparse
import sys


def remove_mentions(text, replace_token):
    return re.sub(r'(?:@[\w_]+)', replace_token, text)


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]


def get_shifts(input_path):
    shifts_dict = {}
    df_shifts = pd.read_csv(input_path, sep=',', encoding='utf8')
    for idx, row in df_shifts.iterrows():
        shifts_dict[row['word']] = row['mean']
    return shifts_dict


def get_durel_shifts(input_path):
    shifts_dict = {}
    df_shifts = pd.read_csv(input_path, sep='\t', encoding='utf8')
    for idx, row in df_shifts.iterrows():
        shifts_dict[row['Lexeme']] = float(row['LSC'])
    return shifts_dict


def add_embedding_to_list(previous, word_emb):
    embeds = [x[0] / x[1] for x in previous]
    cs = list(cosine_similarity(word_emb.reshape(1, -1), np.array(embeds))[0].tolist())
    if len(previous) < 200 and max(cs) < 0.99:
        max_idx = len(previous)
        previous.append((word_emb, 1))
    else:
        max_idx = cs.index(max(cs))
        old_embd, count = previous[max_idx]
        new_embd = old_embd + word_emb
        count = count + 1
        previous[max_idx] = (new_embd, count)
    return previous, max_idx


def tokens_to_batches(ds, tokenizer, batch_size, max_length, target_words, lang, task):
    batches = []
    batch = []
    batch_counter = 0
    sent_tokenizer = nltk.data.load('tokenizers/punkt/english.pickle')
    regextokenizer = RegexpTokenizer('[\(]|[\w-]+|\$[\d\.]+|\S+')
    frequencies = defaultdict(int)

    print('Dataset: ', ds)
    counter = 0
    sent_counter = 0
    sent2count = {}
    count2sent = {}


    with open(ds, 'r', encoding='utf8') as f:

        for line in f:

            if task == 'durel':
                text = line.strip()
            else:
                line = json.loads(line)
                text = line['content']

            if lang != 'German':
                text = text.lower()

            tokenized_words = set(regextokenizer.tokenize(text))

            # print(line)
            contains = False
            for w in target_words:
                if w.strip() in tokenized_words:
                    frequencies[w] += text.count(w)
                    contains = True

            if contains or task in ['aylien', 'durel']:
                tokenized_text = []
                counter += 1
                if counter % 500 == 0:
                    print('Num articles: ', counter)

                for sent in sent_tokenizer.tokenize(text):
                    sent_counter += 1
                    if lang != 'German':
                        lsent = sent.strip().lower()
                    else:
                        lsent = sent.strip()
                    if len(lsent.split()) > 3:
                        marked_sent = "[CLS] " + lsent + " [SEP]"
                        tokenized_sent = tokenizer.tokenize(marked_sent)
                        if len(tokenized_sent) > max_length:
                            tokenized_sent = tokenized_sent[:max_length - 1] + ['[SEP]']
                        sent = tokenizer.convert_tokens_to_string(tokenized_sent)
                        count2sent[sent_counter] = sent
                        sent2count[sent] = sent_counter
                        if len(tokenized_text) + len(tokenized_sent) > max_length:
                            indexed_tokens = tokenizer.convert_tokens_to_ids(tokenized_text)
                            # print("Batch counter: ", len(tokenized_text), batch_counter, tokenized_text)
                            batch.append((indexed_tokens, tokenized_text))
                            batch_counter += 1
                            tokenized_text = tokenized_sent
                            if batch_counter % batch_size == 0:
                                batches.append(batch)
                                batch = []
                        else:
                            tokenized_text.extend(tokenized_sent)

                if len(tokenized_text) > 0:
                    # print("Batch counter: ", len(tokenized_text), batch_counter, tokenized_text)
                    indexed_tokens = tokenizer.convert_tokens_to_ids(tokenized_text)
                    batch.append((indexed_tokens, tokenized_text))
                    batch_counter += 1
                    if batch_counter % batch_size == 0:
                        batches.append(batch)
                        batch = []

    print()
    print("Frequencies", frequencies)
    print('Tokenization done!')
    print('len batches: ', len(batches))

    return batches, count2sent, sent2count, None


def get_token_embeddings(batches, model, batch_size, gpu):
    encoder_token_embeddings = []
    tokenized_text = []
    counter = 0

    for batch in batches:
        counter += 1
        if counter % 1000 == 0:
            print('Generating embedding for batch: ', counter)
        lens = [len(x[0]) for x in batch]
        max_len = max(lens)
        if gpu:
            tokens_tensor = torch.zeros(batch_size, max_len, dtype=torch.long).cuda()
            segments_tensors = torch.ones(batch_size, max_len, dtype=torch.long).cuda()
        else:
            tokens_tensor = torch.zeros(batch_size, max_len, dtype=torch.long)
            segments_tensors = torch.ones(batch_size, max_len, dtype=torch.long)
        batch_idx = [x[0] for x in batch]
        batch_tokens = [x[1] for x in batch]

        for i in range(batch_size):
            length = len(batch_idx[i])
            for j in range(max_len):
                if j < length:
                    tokens_tensor[i][j] = batch_idx[i][j]


        # Predict hidden states features for each layer
        with torch.no_grad():
            model_output = model(tokens_tensor, segments_tensors)
            encoded_layers = model_output[-1][-4:]  # last four layers of the encoder

        for batch_i in range(batch_size):
            encoder_token_embeddings_example = []
            tokenized_text_example = []

            # For each token in the sentence...
            for token_i in range(len(batch_tokens[batch_i])):

                # Holds 12 layers of hidden states for each token
                hidden_layers = []

                # For each of the 12 layers...
                for layer_i in range(len(encoded_layers)):
                    # Lookup the vector for `token_i` in `layer_i`
                    vec = encoded_layers[layer_i][batch_i][token_i]

                    hidden_layers.append(vec)

                hidden_layers = torch.sum(torch.stack(hidden_layers), 0).reshape(1, -1).detach().cpu().numpy()

                encoder_token_embeddings_example.append(hidden_layers)
                tokenized_text_example.append(batch_tokens[batch_i][token_i])

            encoder_token_embeddings.append(encoder_token_embeddings_example)
            tokenized_text.append(tokenized_text_example)

        # Sanity check the dimensions:
        # print("Number of tokens in sequence:", len(token_embeddings))
        # print("Number of layers per token:", len(token_embeddings[0]))

    return encoder_token_embeddings, tokenized_text


def get_slice_embeddings(embeddings_path, datasets, tokenizer, model, batch_size, max_length, lang, target_dict, task, slices, gpu=True):
    vocab_vectors = {}
    count2sents = {}

    for ds_idx, (ds, period) in enumerate(zip(datasets, slices)):
        all_batches, count2sent, sent2count, sent2target_sense = tokens_to_batches(ds, tokenizer, batch_size,
                                                                                   max_length, target_dict, lang, task)

        count2sents[period] = count2sent
        targets = set(list(target_dict.keys()))
        chunked_batches = chunks(all_batches, 1000)
        num_chunk = 0

        for batches in chunked_batches:
            num_chunk += 1
            print('Chunk ', num_chunk)

            # get list of embeddings and list of bpe tokens
            encoder_token_embeddings, tokenized_text = get_token_embeddings(batches, model, batch_size, gpu)

            splitted_tokens = []
            encoder_splitted_array = np.zeros((1, 768))
            prev_token = ""
            encoder_prev_array = np.zeros((1, 768))
            sent_tokens = []

            # go through text token by token
            for example_idx, example in enumerate(tokenized_text):

                for i, token_i in enumerate(example):
                    if token_i == "[CLS]":
                        last_start = i
                    elif token_i == "[SEP]":
                        last_finish = i
                        # print('Example: ', len(example), example)
                        sentence = tokenizer.convert_tokens_to_string(example[last_start:last_finish + 1])
                        # we ignore sents that span across two sequences
                        if sentence.startswith('[CLS]') and sentence.endswith('[SEP]'):
                            # print('Sentence: ', sentence)
                            sentence = sent2count[sentence]
                            # print('Count: ', sentence)
                            for sent_token, sent_idx in sent_tokens:
                                # print(sent_token, count2sent[sentence])
                                if sent_idx in vocab_vectors[sent_token][period + '_text']:
                                    vocab_vectors[sent_token][period + '_text'][sent_idx].append(sentence)
                                else:
                                    vocab_vectors[sent_token][period + '_text'][sent_idx] = [sentence]
                            sent_tokens = []

                    encoder_array = encoder_token_embeddings[example_idx][i]

                    # word is split into parts
                    if token_i.startswith('##') or token_i == '-' or (
                            len(splitted_tokens) > 0 and splitted_tokens[-1] == '-'):

                        # add words prefix (not starting with ##) to the list
                        if prev_token:
                            splitted_tokens.append(prev_token)
                            prev_token = ""
                            encoder_splitted_array = encoder_prev_array

                        # add word to splitted tokens array and add its embedding to splitted_array
                        splitted_tokens.append(token_i)
                        encoder_splitted_array += encoder_array


                    # word is not split into parts
                    else:
                        if token_i in targets:

                            if i == len(example) - 1 or not example[i + 1].startswith('##'):
                                if token_i in vocab_vectors:
                                    # print("In vocab: ", token_i + '_' + period, list(vocab_vectors.keys()))
                                    if period in vocab_vectors[token_i]:
                                        previous = vocab_vectors[token_i][period]
                                        new, new_idx = add_embedding_to_list(previous, encoder_array.squeeze())
                                        vocab_vectors[token_i][period] = new
                                        sent_tokens.append((token_i, new_idx))
                                    else:
                                        vocab_vectors[token_i][period] = [(encoder_array.squeeze(), 1)]
                                        vocab_vectors[token_i][period + '_text'] = {}
                                        sent_tokens.append((token_i, 0))
                                else:
                                    # print("Not in vocab yet: ", token_i + '_' + period, list(vocab_vectors.keys()))
                                    vocab_vectors[token_i] = {period: [(encoder_array.squeeze(), 1)],
                                                              period + '_text': {}}
                                    sent_tokens.append((token_i, 0))
                        # check if there are words in splitted tokens array, calculate average embedding and add the word to the vocabulary
                        if splitted_tokens:

                            encoder_sarray = encoder_splitted_array / len(splitted_tokens)
                            stoken_i = "".join(splitted_tokens).replace('##', '')

                            if stoken_i in targets:
                                if stoken_i in vocab_vectors:
                                    # print("S In vocab: ", stoken_i + '_' + period, list(vocab_vectors.keys()))
                                    if period in vocab_vectors[stoken_i]:
                                        previous = vocab_vectors[stoken_i][period]
                                        new, new_idx = add_embedding_to_list(previous, encoder_sarray.squeeze())
                                        vocab_vectors[stoken_i][period] = new
                                        sent_tokens.append((stoken_i, new_idx))
                                    else:
                                        vocab_vectors[stoken_i][period] = [(encoder_sarray.squeeze(), 1)]
                                        vocab_vectors[stoken_i][period + '_text'] = {}
                                        sent_tokens.append((stoken_i, 0))
                                else:
                                    # print("S Not in vocab yet: ", stoken_i + '_' + period, list(vocab_vectors.keys()))
                                    vocab_vectors[stoken_i] = {period: [(encoder_sarray.squeeze(), 1)],
                                                               period + '_text': {}}
                                    sent_tokens.append((stoken_i, 0))

                            splitted_tokens = []
                            encoder_splitted_array = np.zeros((1, 768))

                        encoder_prev_array = encoder_array
                        prev_token = token_i

            del encoder_token_embeddings
            del tokenized_text
            del batches
            gc.collect()

        print('Sentence embeddings generated.')

    print("Length of vocab after training: ", len(vocab_vectors.items()))

    with open(embeddings_path.split('.')[0] + '.pickle', 'wb') as handle:
        pickle.dump([vocab_vectors, count2sents], handle, protocol=pickle.HIGHEST_PROTOCOL)
    gc.collect()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus_paths",
                        default='data/coha/coha_1960.txt;data/coha/coha_1990.txt',
                        type=str,
                        help="Paths to all corpus time slices separated by ';'.")
    parser.add_argument("--corpus_slices",
                        default='1960;1990',
                        type=str,
                        help="Time slices names separated by ';'. The number should correspond to number of corpus slices paths.")
    parser.add_argument("--target_path", default='data/coha/Gulordava_word_meaning_change_evaluation_dataset.csv', type=str,
                        help="Path to target file")
    parser.add_argument("--task", default='coha', const='all', nargs='?',
                        help="Choose a task", choices=['coha', 'aylien', 'durel'])
    parser.add_argument("--batch_size", default=16, type=int, help="Batch size.")
    parser.add_argument("--max_sequence_length", default=256, type=int)
    parser.add_argument("--gpu", action="store_true", help="Use gpu.")
    parser.add_argument("--path_to_fine_tuned_model", default='models/model_coha_epoch_5/pytorch_model.bin', type=str,
                        help="Path to fine-tuned model. If empty, pretrained model is used")
    parser.add_argument("--embeddings_path", default='embeddings/coha_scalable.pickle', type=str,
                        help="Path to output pickle file containing embeddings.")
    args = parser.parse_args()

    batch_size = args.batch_size
    max_length = args.max_sequence_length
    slices = args.corpus_slices.split(';')

    task = args.task
    tasks = ['coha', 'aylien', 'durel']
    if task not in tasks:
        print("Task not valid, valid choices are: ", ", ".join(tasks))
        sys.exit()
    datasets = args.corpus_paths.split(';')
    if len(args.path_to_fine_tuned_model) > 0:
        fine_tuned = True
    else:
        fine_tuned = False

    datasets = args.corpus_paths.split(';')

    if len(args.path_to_fine_tuned_model) > 0:
        state_dict =  torch.load(args.path_to_fine_tuned_model)

    if task == 'coha':
        lang = 'English'
        shifts_dict = get_shifts(args.target_path)
    elif task == 'aylien':
        lang = 'English'
        shifts_dict = get_shifts(args.target_path)
    elif task == 'durel':
        lang = 'German'
        shifts_dict = get_durel_shifts(args.target_path)


    if lang == 'English':
        tokenizer = BertTokenizer.from_pretrained('bert-base-uncased', do_lower_case=True)
        if fine_tuned:
            state_dict = torch.load(args.path_to_fine_tuned_model)
            model = BertModel.from_pretrained('bert-base-uncased', state_dict=state_dict, output_hidden_states=True)
        else:
            model = BertModel.from_pretrained('bert_base-uncased', output_hidden_states=True)

    elif lang == 'German':
        tokenizer = BertTokenizer.from_pretrained('bert-base-german-cased')
        if fine_tuned:
            state_dict = torch.load(args.path_to_fine_tuned_model)
            model = BertModel.from_pretrained('bert-base-german-cased', state_dict=state_dict, output_hidden_states=True)
        else:
            model = BertModel.from_pretrained('bert-base-german-cased', output_hidden_states=True)

    if args.gpu:
        model.cuda()
    model.eval()

    get_slice_embeddings(args.embeddings_path, datasets, tokenizer, model, batch_size, max_length, lang, shifts_dict, task, slices, gpu=args.gpu)







