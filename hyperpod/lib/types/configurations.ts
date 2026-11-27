export type ClusterVPC = {
  SubnetAZ: string;
  UseFlowLog: boolean;
};
export type ControllerGroup = {
  Name: string;
  Count: number;
  InstanceType: string;
};
export type LoginGroup = {
  Name: string;
  Count: number;
  InstanceType: string;
};
export type WorkerGroup = {
  Name: string;
  Count: number;
  InstanceType: string;
};
export type ClusterInfo = {
  Name: string;
  ControllerGroup: ControllerGroup;
  LoginGroup: LoginGroup;
  WorkerGroup: WorkerGroup[];
};
export type Lustre = {
  S3Prefix: string;
  FileSystemPath: string;
};
export type Mlflow = {
  Enabled: boolean;
};
export type Configuration = {
  StackName: string;
  ClusterVPC: ClusterVPC;
  Cluster: ClusterInfo;
  Lustre: Lustre;
  Mlflow?: Mlflow;
};
