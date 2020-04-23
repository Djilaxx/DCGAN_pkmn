import os
import random
import glob
import re
from pathlib import Path
import numpy as np 

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torchvision.utils as vutils
from torch.utils.tensorboard import SummaryWriter

from backbone.DCGAN import DCGAN_Generator, DCGAN_Discriminator
from utils import weights_init, label_smoothing, noisy_labelling, get_dataloader
from config import config

class DCGAN_train(object):
    '''
    Trainer for the DCGAN model

    Attributes : 
        D - Discriminator is a Torch Discriminator model
        G - Generator is a Torch Generator model
        OptimD - Torch optimizer for D (Adam for this model following paper guideline)
        OptimG - Torch optimizer for G (Adam too)
        dataloader : Torch object to load data from a dataset  
    '''
    def __init__(self, Generator, Discriminator):
        print("Training DCGAN model.")
        self.device =  torch.device("cuda:0" if (torch.cuda.is_available() and config.DATA.ngpu > 0) else "cpu")
        self.writer = SummaryWriter('runs/DCGAN_pokemon')
        self.fix_noise = torch.randn(64, config.DATA.nz, 1, 1, device = self.device)
        self.dataloader = get_dataloader()

        self.G = Generator(config.DATA.ngpu).to(self.device)
        self.G.apply(weights_init)
        self.D = Discriminator(config.DATA.ngpu).to(self.device)
        self.D.apply(weights_init)

        self.loss = nn.BCELoss()

        self.optimD = optim.Adam(self.D.parameters(), lr = config.TRAIN.dcgan.lr, betas=(config.TRAIN.dcgan.beta1, config.TRAIN.dcgan.beta2))
        self.optimG = optim.Adam(self.G.parameters(), lr = config.TRAIN.dcgan.lr, betas=(config.TRAIN.dcgan.beta1, config.TRAIN.dcgan.beta2))

    def train(self, checkpoint = "last"):
        real_label = 1
        fake_label = 0
        iters = 0

        for epoch in range(config.DATA.num_epochs):
            for i, data in enumerate(self.dataloader, 0):

                #--------------------------------
                # UPDATE DISCRIMINATOR NETWORK
                #--------------------------------

                # TRAIN DISCRIMINATOR ON REAL DATA
                self.D.zero_grad()
                real_data = data[0].to(self.device)
                b_size = real_data.size(0) #Computing batch size again because it might differ at the end of epoch
                label = torch.full((b_size,), real_label, device=self.device)
                label = label_smoothing(label, real = True)
                label = noisy_labelling(label, 0.05, (0,0.2), smooth_label=True)
                
                output = self.D(real_data).view(-1)
                errD_real = self.loss(output, label)
                errD_real.backward()
                D_x = output.mean().item()

                # TRAIN DISCRIMINATOR ON FAKE DATA
                noise = torch.randn(b_size, config.DATA.nz, 1, 1, device = self.device)
                
                fake = self.G(noise)
                label.fill_(fake_label)
                label = label_smoothing(label, real = False)
                label = noisy_labelling(label, 0.05, (0.7, 1.2), smooth_label=True)

                output = self.D(fake.detach()).view(-1)
                errD_fake = self.loss(output,label)
                errD_fake.backward()
                D_G_z1 = output.mean().item()
                errD = errD_real + errD_fake

                # TAKE AN OPTIMIZER STEP FOR DISCRIMINATOR
                self.optimD.step()

                #--------------------------
                # TRAIN GENERATOR NETWORK
                #--------------------------
                self.G.zero_grad()
                label.fill_(real_label)

                output = self.D(fake).view(-1)
                errG = self.loss(output,label)
                errG.backward()
                D_G_z2 = output.mean().item()
                
                self.optimG.step()

                #------------------------
                # SAVING AND METRICS
                #------------------------

                # PRINT TRAINING INFO AND SAVE LOSS
                if i % 25 == 0:
                    print('[%d/%d][%d/%d]\tLoss_D: %.4f\tLoss_G: %.4f\tD(x): %.4f\tD(G(z)): %.4f / %.4f'
                        % (epoch, config.DATA.num_epochs, i, len(self.dataloader),
                            errD.item(), errG.item(), D_x, D_G_z1, D_G_z2))

                    self.writer.add_scalar("Generator Loss", errG.item(), global_step= epoch * len(self.dataloader) + i)
                    self.writer.add_scalar("Discriminator Loss", errD.item(), global_step= epoch * len(self.dataloader) + i )

                if (iters % 500 == 0) or ((epoch == config.DATA.num_epochs-1) and (i == len(self.dataloader)-1)):
                    with torch.no_grad():
                        fake_grid = self.G(self.fix_noise).detach().cpu()
                    self.writer.add_image("fake pokemons", fake_grid, global_step= epoch * len(self.dataloader) + i, dataformats="NCHW")
                
                # MODEL CHECKPOINT FOR FURTHER TRAINING OR EVALUATION
                if checkpoint != "none":
                    last_iter = ((epoch == config.DATA.num_epochs-1) and (i == len(self.dataloader)-1))
                
                    if checkpoint == "last" and last_iter:
                        save_cp()

                    elif checkpoint == "few":
                        cp_array = np.array([0.25, 0.5, 0.75])
                        cp_epoch = cp_array*config.DATA.num_epochs
                        if epoch in cp_epoch.astype(int) or last_iter:
                            save_cp()

                    elif checkpoint == "often":
                        cp_array = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
                        cp_epoch = cp_array*config.DATA.num_epochs
                        if epoch in cp_epoch.astype(int) or last_iter:
                            save_cp()
                else:
                    pass

                iters += 1

        def save_cp():
            '''
            Save G & D in the checkpoint repository
            '''
            Path("checkpoint/").mkdir(parents=True, exist_ok=True)

            torch.save({
            'epoch': epoch,
            'model_state_dict': self.G.state_dict(),
            'optimizer_state_dict': self.optimG.state_dict(),
            'loss': errG
            }, "checkpoint/checkpointG-" + str(epoch) + '-' + str(round(errG.item(),2)) + '.pt')
            
            torch.save({
            'epoch': epoch,
            'model_state_dict': self.D.state_dict(),
            'optimizer_state_dict': self.optimD.state_dict(),
            'loss': errD
            }, 'checkpoint/checkpointD-' + str(epoch) + '-' + str(round(errD.item(),2)) + '.pt')



    def evaluate(self, type = "batch"):

        def load_G(file):
            self.G.Generator(config.DATA.ngpu).to(self.device)
            cp = torch.load(file)
            self.G.load_state_dict(cp["model_state_dict"])
            self.G.eval()
            return self.G
        
        def create_fake(b_s):
            noise = torch.randn(b_s, config.DATA.nz, 1, 1, device = self.device)
            with torch.no_grad:
                fake = self.G(noise).detach().cpu()
            for i in range(0, len(fake)):
                img = fake[i]
                vutils.save_image(img, "results/" + name + "/" + name + "_" + str(i) + ".png")

        if not glob.glob("checkpoint/"):
            raise Exception("No checkpoint, train first")
        
        Path("results/").mkdir(parents=True, exist_ok=True)
        for file in sorted(glob.glob("checkpoint/checkpointG*"), key=os.path.getmtime):
            name = re.findall(r'[^\\/]+|[\\/]', file)[2]
            if type == "batch" and file == sorted(glob.glob("checkpoint/checkpointG*"), key=os.path.getmtime)[-1]:
                self.G = load_G(file)
                create_fake(64)
            elif type == "full":
                self.G = load_G(file)
                create_fake(64)
            elif type == "one" and file == sorted(glob.glob("checkpoint/checkpointG*"), key=os.path.getmtime)[-1]:
                self.G = load_G(file)
                create_fake(1)
            else:
                raise Exception("Unknown evaluation type")