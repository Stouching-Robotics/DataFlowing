#!/usr/bin/env python3
"""SE(3), not scale-corrected, position/orientation evaluation with coverage."""
import argparse
import json
from pathlib import Path
import numpy as np
from scipy.spatial.transform import Rotation, Slerp


def rigid_alignment(estimated, truth):
    e0, g0 = estimated.mean(axis=0), truth.mean(axis=0)
    u, singular, vt = np.linalg.svd((estimated-e0).T @ (truth-g0))
    sign = np.eye(3)
    sign[-1, -1] = np.linalg.det(vt.T @ u.T)
    rotation = vt.T @ sign @ u.T
    translation = g0 - rotation @ e0
    denominator = np.square(estimated-e0).sum()
    scale_diagnostic = float((singular * np.diag(sign)).sum() / denominator) if denominator else None
    return rotation, translation, scale_diagnostic


def statistics(values):
    values = np.asarray(values)
    if len(values) == 0:
        return None
    return dict(count=len(values), rmse=float(np.sqrt(np.mean(values**2))),
                mean=float(values.mean()), median=float(np.median(values)),
                p95=float(np.percentile(values,95)), max=float(values.max()))


def match_truth(est_t, gt, max_gap=.03):
    # Interpolate ground truth at estimate timestamps. Never extrapolate or
    # interpolate through mocap dropouts; report rejected estimates separately.
    t=gt[:,0]
    hi=np.searchsorted(t,est_t,side='right')
    lo=np.maximum(0,hi-1); hi=np.minimum(len(t)-1,hi)
    valid=(est_t>=t[0])&(est_t<=t[-1])&((t[hi]-t[lo])<=max_gap)
    target=est_t[valid]
    position=np.stack([np.interp(target,t,gt[:,k]) for k in (1,2,3)],axis=1)
    rotation=Slerp(t-t[0],Rotation.from_quat(gt[:,4:8]))(target-t[0]).as_matrix()
    return valid, position, rotation


def evaluate_trajectory(estimate, gt):
    if len(estimate)<3:
        raise ValueError('Fewer than three trajectory poses')
    if not np.isfinite(estimate).all() or np.any(np.diff(estimate[:,0])<=0):
        raise ValueError('Non-finite or non-monotonic estimated trajectory')
    valid,gp,gR=match_truth(estimate[:,0],gt)
    if valid.sum()<3:
        raise ValueError('Insufficient ground-truth overlap')
    estimate=estimate[valid]
    eR=Rotation.from_quat(estimate[:,4:8]).as_matrix()
    rot,offset,scale=rigid_alignment(estimate[:,1:4],gp)
    aligned=estimate[:,1:4]@rot.T+offset
    errors=np.linalg.norm(aligned-gp,axis=1)
    angle=Rotation.from_matrix(np.transpose(gR,(0,2,1))@(rot@eR)).magnitude()*180/np.pi
    times=estimate[:,0]
    # One-second full-pose RPE; relative translations are expressed in each
    # source pose's local coordinates, not merely differences of world vectors.
    idx=np.arange(len(times));j=np.searchsorted(times,times+1.)
    j=np.minimum(j,len(times)-1)
    previous=np.maximum(j-1,0)
    closer=np.abs(times[previous]-times-1.)<np.abs(times[j]-times-1.)
    j=np.where(closer,previous,j)
    use=(j>idx)&(np.abs(times[j]-times-1.)<=.03);i=idx[use];j=j[use]
    de=np.einsum('nij,nj->ni',np.transpose(eR[i],(0,2,1)),estimate[j,1:4]-estimate[i,1:4])
    dg=np.einsum('nij,nj->ni',np.transpose(gR[i],(0,2,1)),gp[j]-gp[i])
    rel_e=np.transpose(eR[i],(0,2,1))@eR[j]
    rel_g=np.transpose(gR[i],(0,2,1))@gR[j]
    rpe_angle=Rotation.from_matrix(np.transpose(rel_g,(0,2,1))@rel_e).magnitude()*180/np.pi if len(i) else []
    result={
        'alignment':'SE3 rigid, scale fixed at 1', 'associated_poses':len(estimate),
        'groundtruth_match_ratio':float(valid.mean()),
        'ate_translation_m':statistics(errors), 'absolute_rotation_deg':statistics(angle),
        'rpe_1s_translation_m':statistics(np.linalg.norm(de-dg,axis=1)),
        'rpe_1s_rotation_deg':statistics(rpe_angle),
        'diagnostic_optimal_scale_not_applied':scale,
        'duration_s':float(times[-1]-times[0]),
        'groundtruth_path_length_m':float(np.linalg.norm(np.diff(gp,axis=0),axis=1).sum()),
        'endpoint_error_m':float(errors[-1]),
    }
    return result, np.column_stack((times,aligned,gp,errors))


def load_gt(path, transform=None):
    data=np.loadtxt(path,delimiter=',',comments='#',ndmin=2)
    # TUM-VI EuRoC CSV: timestamp(ns), x,y,z,qw,qx,qy,qz.
    gt=np.column_stack((data[:,0]/1e9,data[:,1:4],data[:,5:8],data[:,4]))
    if not np.isfinite(gt).all() or np.any(np.diff(gt[:,0])<=0):
        raise ValueError('Invalid ground truth')
    if transform is not None:
        # T_BS maps mocap sensor S into IMU body B. T_WB=T_WS*inverse(T_BS).
        inverse=np.linalg.inv(transform)
        Rs=Rotation.from_quat(gt[:,4:8]).as_matrix()
        gt[:,1:4]+=np.einsum('nij,j->ni',Rs,inverse[:3,3])
        gt[:,4:8]=Rotation.from_matrix(Rs@inverse[:3,:3]).as_quat()
    return gt


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--groundtruth',type=Path,required=True)
    parser.add_argument('--gt-body-from-sensor',type=Path,
                        help='JSON 4x4 T_BS if GT is a separate marker frame')
    parser.add_argument('--results',type=Path,required=True)
    args=parser.parse_args()
    transform=np.array(json.loads(args.gt_body_from_sensor.read_text())) if args.gt_body_from_sensor else None
    gt=load_gt(args.groundtruth,transform)
    reports=[]
    for case in sorted(args.results.iterdir()):
        if not (case/'process_result.json').exists():continue
        report=json.loads((case/'process_result.json').read_text())
        frames=np.genfromtxt(case/'frames.csv',delimiter=',',names=True,ndmin=1)
        n=len(frames); ok=frames['state']==2
        report['processed_frames']=n
        report['logged_frames']=n
        report['partial_result']=not report['clean_completion']
        # A crash can leave the tail of native buffered logs unwritten.
        report['logged_frame_count_is_lower_bound']=not report['clean_completion']
        report['tracking_ok_ratio']=float(ok.mean()) if n else 0.
        if n:
            first_ok=np.flatnonzero(ok)
            report['tracking_ok_ratio_after_first_ok']=float(ok[first_ok[0]:].mean()) if len(first_ok) else 0.
            report['first_tracking_s']=float((frames['timestamp_ns'][first_ok[0]]-frames['timestamp_ns'][0])/1e9) if len(first_ok) else None
            report['tracking_states']={str(int(k)):int((frames['state']==k).sum()) for k in np.unique(frames['state'])}
            report['track_ms']=statistics(frames['track_ms'])
            report['track_over_50ms_ratio']=float((frames['track_ms']>50).mean())
            report['injected_frames']=int(frames['injected'].sum())
            report['peak_rss_mib']=float(frames['max_rss_kib'].max()/1024)
            if report['mode']!='normal':
                elapsed=(frames['timestamp_ns']-frames['timestamp_ns'][0])/1e9
                end=report['fault_start_s']+report['fault_duration_s']
                after=np.flatnonzero(elapsed>=end)
                report['post_fault_tracking_ok_ratio']=float(ok[after].mean()) if len(after) else None
                report['recovery_delay_s_10_consecutive_ok']=None
                for i in after:
                    if i+10<=len(ok) and ok[i:i+10].all():
                        report['recovery_delay_s_10_consecutive_ok']=float(elapsed[i]-end)
                        break
        for leaf,timestamp_scale in [('optimized_body_ns.txt',1e9),('online_body.txt',1.)]:
            path=case/leaf
            if not path.exists() or not path.stat().st_size:
                report[leaf]={'error':'No saved trajectory'};continue
            try:
                estimate=np.loadtxt(path,ndmin=2);estimate[:,0]/=timestamp_scale
                result,aligned=evaluate_trajectory(estimate,gt)
                result['poses_over_processed_frames']=len(estimate)/n if n else 0.
                result['sequence_completed']=report['clean_completion']
                report[leaf]=result
                # Keep full-sequence metrics above; also report a clearly labelled
                # post-startup diagnostic, never silently exclude IMU initialization.
                if len(estimate)>3 and estimate[-1,0]-estimate[0,0]>6:
                    stable=estimate[estimate[:,0]>=estimate[0,0]+5.]
                    result['after_first_tracked_pose_plus_5s'],_=evaluate_trajectory(stable,gt)
                np.savetxt(case/(leaf+'.aligned.csv'),aligned,delimiter=',',
                           header='timestamp,ex,ey,ez,gx,gy,gz,error_m')
            except (ValueError,IndexError) as exc:
                report[leaf]={'error':str(exc)}
        log=(case/'native.log').read_text(errors='replace')
        report['log_counts']={s:log.count(s) for s in ('Creation of new map','IMU is not or recently initialized',
                                                      'Reset map','TRACK_REF_KF','New Map created')}
        (case/'evaluation.json').write_text(json.dumps(report,indent=2))
        reports.append(report)
    (args.results/'summary.json').write_text(json.dumps(reports,indent=2))
    print(json.dumps(reports,indent=2))


if __name__=='__main__':main()
