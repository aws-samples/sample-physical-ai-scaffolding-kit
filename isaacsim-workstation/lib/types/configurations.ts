export type Configuration = {
  StackPrefix: string;
  VpcId: string;
  SubnetAZ: string;
  InstanceType: string;
  /** IPv4 CIDR ranges allowed to connect to Amazon DCV (TCP 8443). */
  AllowedIpv4Cidrs: string[];
};
