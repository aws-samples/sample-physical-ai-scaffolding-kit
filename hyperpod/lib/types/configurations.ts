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
export type Vams = {
  Enabled: boolean;
  ConfigName: string;
  AssetBucketNames: string[];
  WorkflowBucketName: string;
  SeparateLoginRole?: boolean;
  KmsKeyArns?: string[];
  QueueVisibilityTimeoutSeconds?: number;
  MaxReceiveCount?: number;
  HeartbeatIntervalSeconds?: number;
  AbortGraceSeconds?: number;
  LeaseSeconds?: number;
  ItemTtlDays?: number;
  StagingExpirationDays?: number;
  IngestWaitSeconds?: number;
  MaxConcurrentRuns?: number;
  SlurmPartition?: string;
  MlflowExperimentName?: string;
};
export type Configuration = {
  StackName: string;
  ClusterVPC: ClusterVPC;
  Cluster: ClusterInfo;
  Lustre: Lustre;
  Mlflow?: Mlflow;
  Vams?: Vams;
};
