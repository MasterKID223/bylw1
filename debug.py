import torch

# 假设evokg_embs是一个形状为[500, 200]的PyTorch张量
evokg_embs = torch.randn(2, 3)  # 示例数据

# # indexes是想要从中抽取实体特征的实体索引列表或张量
# indexes = [1, 1, 1, 1]  # 示例索引
# indexes = torch.tensor(indexes)  # 如果indexes已经是张量，则不需要这一步
#
# # 使用index_select函数从evokg_embs中选取对应indexes的行
# selected_embs = torch.index_select(evokg_embs, 0, indexes)
#
# print(selected_embs.size())
#
rel_embs = torch.randn(2, 3, 2)  # 示例数据
tmp = rel_embs[:, :, 0]
pass
