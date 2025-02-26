#!/usr/bin/env python
"""Preprocess a KGE dataset into a the format expected by libkge.

Call as `preprocess.py --folder <name>`. The original dataset should be stored in
subfolder `name` and have files "train.txt", "valid.txt", and "test.txt". Each file
contains one SPO triple per line, separated by tabs.

During preprocessing, each distinct entity name and each distinct distinct relation name
is assigned an index (dense). The index-to-object mapping is stored in files
"entity_map.del" and "relation_map.del", resp. The triples (as indexes) are stored in
files "train.del", "valid.del", and "test.del". Metadata information is stored in a file
"dataset.yaml".

"""

import argparse
import yaml
import os.path
import numpy as np
from collections import OrderedDict, defaultdict

def store_map(symbol_map, filename):
    with open(filename, "w", encoding="utf-8") as f:
        for symbol, index in symbol_map.items():
            f.write(f"{index}\t{symbol}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=str)
    parser.add_argument("--order_sop", action="store_true")
    args = parser.parse_args()

    print(f"Preprocessing {args.folder}...")
    raw_split_files = {"train": "train.txt", "valid": "valid.txt", "test": "test.txt"}
    split_files = {"train": "train.del", "valid": "valid.del", "test": "test.del"}
    string_files = {"entity_strings": "entity_strings.del", "relation_strings": "relation_strings.del", "time_strings": "time_strings.del"}
    split_files_without_unseen = {"train_sample": "train_sample.del", "valid_without_unseen": "valid_without_unseen.del", 
            "test_without_unseen": "test_without_unseen.del"}
    split_sizes = {}

    if args.order_sop:
        S, P, O = 0, 2, 1
    else:
        S, P, O, T = 0, 1, 2, 3

    # read data and collect entities and relations
    raw = {}
    entities = {}
    relations = {}
    times = {}
    entities_in_train = {}
    relations_in_train = {}
    ent_id = 0
    rel_id = 0
    tim_id = 0
    year_list = []
    start_year= -500
    end_year = 3000
    times_in_train = None
    for split, filename in raw_split_files.items():
        with open(args.folder + "/" + filename, "r", encoding="utf-8") as f:
            raw[split] = list(map(lambda s: s.strip().split("\t"), f.readlines()))
            for t in raw[split]:
                if t[S] not in entities:
                    entities[t[S]] = ent_id
                    ent_id += 1
                if t[P] not in relations:
                    relations[t[P]] = rel_id
                    rel_id += 1
                if t[O] not in entities:
                    entities[t[O]] = ent_id
                    ent_id += 1
                if t[T][0] == '-':
                    start = -int(t[T].split('-')[1])
                    year_list.append(start)
                else:
                    start = t[T].split('-')[0]
                    if start == '####':
                        start = start_year
                    else:
                        start = start.replace('#', '0')
                        start = int(start)
                    year_list.append(start)
                times[t[T]] = start
                if t[T+1][0] == '-':
                    end = -int(t[T].split('-')[1])
                    year_list.append(end)
                else:
                    end = t[T+1].split('-')[0]
                    if end == '####':
                        end = end_year
                    else:
                        end = end.replace('#', '0')
                        end = int(end)
                    year_list.append(end)
                times[t[T+1]] = end
            print(
                f"Found {len(raw[split])} triples in {split} split "
                f"(file: {filename})."
            )
            split_sizes[split] = len(raw[split])
            if "train" in split:
                entities_in_train = entities.copy()
                relations_in_train = relations.copy()
                times_in_train = times.copy()
                # times_in_train = year_list.copy()
    year_list.sort()
    freq = defaultdict(int)
    for y in year_list:
        freq[y] = freq[y]+1
    year_class = []
    count = 0
    for key in sorted(freq.keys()):
        count += freq[key]
        if count >= 300:
            year_class.append(key)
            count = 0
    year_class[-1] = year_list[-1]
    year2id = {}
    prev_year = year_list[0]
    i = 0
    for i, yr in enumerate(year_class):
        year2id[(prev_year, yr)]=i
        prev_year = yr + 1
        
    print(f"{len(relations)} distinct relations")
    print(f"{len(entities)} distinct entities")
    print(f"{len(year2id.keys())} distinct time")
    print("Writing relation and entity map...")
    store_map(relations, os.path.join(args.folder, "relation_ids.del"))
    store_map(entities, os.path.join(args.folder, "entity_ids.del"))
    store_map(year2id, os.path.join(args.folder, "time_ids.del"))
    print("Done.")

    # write out triples using indexes
    print("Writing triples...")
    without_unseen_sizes = {}
    for split, filename in split_files.items():
        if split in ["valid", "test"]:
            split_without_unseen = split + "_without_unseen"
            f_wo_unseen = open(os.path.join(args.folder, 
                                split_files_without_unseen[split_without_unseen]), "w")
        else:
            split_without_unseen = split + "_sample"
            f_tr_sample = open(os.path.join(args.folder, 
                                split_files_without_unseen[split_without_unseen]), "w")
            train_sample = np.random.choice(split_sizes["train"], split_sizes["valid"], False)
        with open(os.path.join(args.folder, filename), "w") as f:
            size_unseen = 0
            for n, t in enumerate(raw[split]):
                start2id, end2id = times[t[T]], times[t[T+1]]
                for key, time_idx in sorted(year2id.items(), key=lambda x:x[1]):
                    if start2id>=key[0] and start2id<=key[1]:
                        start2id = time_idx
                    if end2id>=key[0] and end2id<=key[1]:
                        end2id = time_idx
                if start2id == end2id:
                    f.write(f'{str(entities[t[S]])}\t{str(relations[t[P]])}\t{str(entities[t[O]])}\t{str(start2id)}\n')
                else:
                    f.write(f'{str(entities[t[S]])}\t{str(relations[t[P]])}\t{str(entities[t[O]])}\t{str(start2id)}\n')
                    f.write(f'{str(entities[t[S]])}\t{str(relations[t[P]])}\t{str(entities[t[O]])}\t{str(end2id)}\n')
                if split == "train" and n in train_sample:
                    if start2id == end2id:
                        f_tr_sample.write(f'{str(entities[t[S]])}\t{str(relations[t[P]])}\t{str(entities[t[O]])}\t{str(start2id)}\n')
                        size_unseen += 1
                    else:
                        f_tr_sample.write(f'{str(entities[t[S]])}\t{str(relations[t[P]])}\t{str(entities[t[O]])}\t{str(start2id)}\n')
                        f_tr_sample.write(f'{str(entities[t[S]])}\t{str(relations[t[P]])}\t{str(entities[t[O]])}\t{str(end2id)}\n')
                        size_unseen += 2
                elif split in ["valid", "test"] and t[S] in entities_in_train and \
                    t[O] in entities_in_train and t[P] in relations_in_train:
                    if start2id == end2id:
                        f_wo_unseen.write(f'{str(entities[t[S]])}\t{str(relations[t[P]])}\t{str(entities[t[O]])}\t{str(start2id)}\n')
                        size_unseen += 1
                    else:
                        f_wo_unseen.write(f'{str(entities[t[S]])}\t{str(relations[t[P]])}\t{str(entities[t[O]])}\t{str(start2id)}\n')
                        f_wo_unseen.write(f'{str(entities[t[S]])}\t{str(relations[t[P]])}\t{str(entities[t[O]])}\t{str(end2id)}\n')
                        size_unseen += 2
            without_unseen_sizes[split_without_unseen] = size_unseen

    # write config
    print("Writing dataset.yaml...")
    dataset_config = dict(
        name=args.folder,
        num_entities=len(entities),
        num_relations=len(relations),
        num_times=len(year2id)
    )
    for obj in [ "entity", "relation", "time" ]:
        dataset_config[f"files.{obj}_ids.filename"] = f"{obj}_ids.del"
        dataset_config[f"files.{obj}_ids.type"] = "map"
    for split in split_files.keys():
        dataset_config[f"files.{split}.filename"] = split_files.get(split)
        dataset_config[f"files.{split}.type"] = "triples"
        dataset_config[f"files.{split}.size"] = split_sizes.get(split)
    for split in split_files_without_unseen.keys():
        dataset_config[f"files.{split}.filename"] = split_files_without_unseen.get(split)
        dataset_config[f"files.{split}.type"] = "triples"
        dataset_config[f"files.{split}.size"] = without_unseen_sizes.get(split)
    for string in string_files.keys():
        if os.path.exists(os.path.join(args.folder, string_files[string])):
            dataset_config[f"files.{string}.filename"] = string_files.get(string)
            dataset_config[f"files.{string}.type"] = "idmap"
    print(yaml.dump(dict(dataset=dataset_config)))
    with open(os.path.join(args.folder, "dataset.yaml"), "w+") as filename:
        filename.write(yaml.dump(dict(dataset=dataset_config)))
