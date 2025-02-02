import yaml
import numpy as np

# Read trace.yaml file
hits_metrics = {
    'hits_at_1': [],
    'hits_at_3': [], 
    'hits_at_10': [],
    'mean_reciprocal_rank': []
}

with open('log_process/trace.yaml', 'r') as f:
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

# Calculate statistics
for metric, values in hits_metrics.items():
    if values:
        print(f"\n{metric}:")
        print(f"Max: {max(values):.4f}")
        print(f"Min: {min(values):.4f}") 
        print(f"Mean: {np.mean(values):.4f}")
