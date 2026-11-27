export type ImageConfig = {
  readonly baseImage: string;
  readonly buildInstanceType: string;
  readonly diskSizeGb: number;
  readonly parameters?: Record<string, Record<string, string>>;
};

export type Configuration = {
  VpcId: string;
  DefaultInstanceType: string;
  ImageNames: string[];
  /** IPv4 CIDR ranges allowed to connect to Amazon DCV (TCP/UDP 8443). */
  AllowedIpv4Cidrs: string[];
};
