import os

if __name__ == '__main__':
    data_name = "ICEWS14"
    # with open(f"F:\\code\\bylw\\TKGElib-mycode\\kge\\model\\evokg_model\\data\\{data_name}\\train.del", "r") as f:
    #     # 把这个文件中的每一行的最后一个位置加\t，然后再加0，重新保存为test.txt
    #     for line in f.readlines():
    #         line = line.strip()
    #         line = line + "\t" + "0"
    #         print(line)
    #         with open(f"F:\\code\\bylw\\TKGElib-mycode\\kge\\model\\evokg_model\\data\\{data_name}\\train.txt", "a") as f:
    #             f.write(line + "\n")
    #
    # with open(f"F:\\code\\bylw\\TKGElib-mycode\\kge\\model\\evokg_model\\data\\{data_name}\\valid.del", "r") as f:
    #     # 把这个文件中的每一行的最后一个位置加\t，然后再加0，重新保存为test.txt
    #     for line in f.readlines():
    #         line = line.strip()
    #         line = line + "\t" + "0"
    #         print(line)
    #         with open(f"F:\\code\\bylw\\TKGElib-mycode\\kge\\model\\evokg_model\\data\\{data_name}\\valid.txt", "a") as f:
    #             f.write(line + "\n")
    #
    # with open(f"F:\\code\\bylw\\TKGElib-mycode\\kge\\model\\evokg_model\\data\\{data_name}\\test.del", "r") as f:
    #     # 把这个文件中的每一行的最后一个位置加\t，然后再加0，重新保存为test.txt
    #     for line in f.readlines():
    #         line = line.strip()
    #         line = line + "\t" + "0"
    #         print(line)
    #         with open(f"F:\\code\\bylw\\TKGElib-mycode\\kge\\model\\evokg_model\\data\\{data_name}\\test.txt", "a") as f:
    #             f.write(line + "\n")

    # 往stat.txt中写入500\t20\t0
    with open(f"F:\\code\\bylw\\TKGElib-mycode\\kge\\model\\evokg_model\\data\\{data_name}\\stat.txt", "w") as f:
        f.write("7128\t230\t0")

