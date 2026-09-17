#!/usr/bin/env python3
"""Generate diagnostic plots from completed evaluations; never opens a GUI."""
import argparse
import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    parser=argparse.ArgumentParser();parser.add_argument('results',type=Path)
    args=parser.parse_args()
    for case in args.results.iterdir():
        evaluation=case/'evaluation.json'
        if not evaluation.exists():continue
        report=json.loads(evaluation.read_text())
        optimized=case/'optimized_body_ns.txt.aligned.csv'
        source=optimized if optimized.exists() else case/'online_body.txt.aligned.csv'
        if not source.exists():continue
        data=np.loadtxt(source,delimiter=',',ndmin=2)
        frames=np.genfromtxt(case/'frames.csv',delimiter=',',names=True,ndmin=1)
        fig,axes=plt.subplots(2,2,figsize=(12,8))
        ax=axes[0,0];ax.plot(data[:,4],data[:,5],label='ground truth',color='black');ax.plot(data[:,1],data[:,2],label='estimate (SE3 aligned)',alpha=.8)
        ax.set(xlabel='x [m]',ylabel='y [m]',title='XY trajectory');ax.axis('equal');ax.legend()
        ax=axes[0,1];ax.plot(data[:,0]-data[0,0],data[:,-1]*100)
        ax.set(xlabel='time [s]',ylabel='position error [cm]',title='ATE: scale fixed at 1')
        elapsed=(frames['timestamp_ns']-frames['timestamp_ns'][0])/1e9
        ax=axes[1,0];ax.plot(elapsed,frames['track_ms'],linewidth=.5);ax.axhline(50,color='red',ls='--',label='20 Hz budget')
        ax.set(xlabel='dataset time [s]',ylabel='TrackStereo [ms]',title='Processing latency');ax.legend()
        ax=axes[1,1];ax.plot(elapsed,frames['max_rss_kib']/1024)
        ax.set(xlabel='dataset time [s]',ylabel='peak RSS [MiB]',title='Memory high-water mark (not a leak diagnosis)')
        if report['mode']!='normal':
            for ax in (axes[1,0],axes[1,1]):ax.axvspan(report['fault_start_s'],report['fault_start_s']+report['fault_duration_s'],alpha=.3,color='orange')
        for ax in axes.flat:ax.grid(alpha=.25)
        fig.suptitle(f"{case.name}: exit={report['returncode']}, tracking OK={report['tracking_ok_ratio']:.2%}; {'optimized' if optimized.exists() else 'online'} trajectory")
        fig.tight_layout();fig.savefig(case/'diagnostics.png',dpi=150);plt.close(fig)


if __name__=='__main__':main()
