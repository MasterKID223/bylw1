import yaml
import numpy as np

# Read trace.yaml file
hits_metrics = {
    'hits_at_1': [],
    'hits_at_3': [],
    'hits_at_10': [],
    'hits_at_1_M-N': [],
    'hits_at_3_M-N': [],
    'hits_at_10_M-N': [],
    'hits_at_1_filtered': [],
    'hits_at_3_filtered': [],
    'hits_at_10_filtered': [],
    'hits_at_1_filtered_M-N': [],
    'hits_at_3_filtered_M-N': [],
    'hits_at_10_filtered_M-N': [],
    'hits_at_1_filtered_with_test': [],
    'hits_at_3_filtered_with_test': [],
    'hits_at_10_filtered_with_test': [],
    'hits_at_1_filtered_with_test_M-N': [],
    'hits_at_3_filtered_with_test_M-N': [],
    'hits_at_10_filtered_with_test_M-N': [],
    'mean_reciprocal_rank': []
}

with open('log_process/trace-gdelt-gcn-163.yaml', 'r') as f:
    for line in f:
        try:
            entry = yaml.safe_load(line)
            if entry.get('event') == 'eval_completed':
                # Collect hits metrics
                for metric in hits_metrics.keys():
                    if metric in entry:
                        hits_metrics[metric].append(entry[metric])
        except yaml.YAMLError:
            continue

for metric, values in hits_metrics.items():
    if values:
        print(f"\n{metric}:")
        print(f"Max: {max(values):.4f}")
        print(f"Min: {min(values):.4f}")
        print(f"Mean: {np.mean(values):.4f}")