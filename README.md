# ExPred

1. This is the implementation of the paper [Explain and Predict, and then Predict Again](https://dl.acm.org/doi/abs/10.1145/3437963.3441758) (accepted in WSDM2021). 
2. This code is implemented based on the pipeline model of the [Eraserbenchmark](http://www.eraserbenchmark.com/). All data used by the model can be found from the Eraser Benchmark, too.
3. I just perform the experiments on this code using the different hyperparameters and reproduce the results.

## Usage:
  1. Install the required packages from the **enviroment.yml** using the command **conda env create -f environment.yml** , after that activate the enviroment.
  2. Change **"--data_dir"** to **"data/{movies/fever/multirc}"** and **"--conf"** to **"params/{movies,fever,multirc}_expred.json"** .
  3. Run the file **train.py** using **python train.py** .
  
  
**Note: **
1. Depending on your hardware you may have to change the `batch_size` in the config file. 
2. For _fever_ dataset , you have to use a **scheduler**. So, to use it uncomment line no **104** and **105** in **mtl_evidence_classifier.py** . 
