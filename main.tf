terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

resource "aws_instance" "monitor_target" {
  ami                    = "ami-0c02fb55956c7d316" # Amazon Linux 2, us-east-1
  instance_type          = "t3.micro"
  key_name               = aws_key_pair.sre_key.key_name
  vpc_security_group_ids = [aws_security_group.sre_sg.id]

  user_data = <<-EOF
    #!/bin/bash
    yum update -y
    yum install -y docker
    systemctl start docker
    systemctl enable docker
    usermod -aG docker ec2-user
    mkdir -p /home/ec2-user/sre-project
    chown -R ec2-user:ec2-user /home/ec2-user/sre-project
    mkdir -p /usr/local/lib/docker/cli-plugins
    curl -SL https://github.com/docker/buildx/releases/download/v0.13.1/buildx-v0.13.1.linux-amd64 -o /usr/local/lib/docker/cli-plugins/docker-buildx
    chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
    curl -SL https://github.com/docker/compose/releases/download/v2.24.6/docker-compose-linux-x86_64 -o /usr/local/lib/docker/cli-plugins/docker-compose
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  EOF

  tags = {
    Name = "sre-project"
  }
}

# Key pair - uploads public key to AWS
resource "aws_key_pair" "sre_key" {
  key_name   = "sre-key"
  public_key = file("C:\\Users\\shibl\\.ssh\\sre-key.pub")
}

# Security group - opens SSH (22), HTTP (80), API (8000), Prometheus (9090), and Grafana (3000) ports
resource "aws_security_group" "sre_sg" {
  name = "sre-monitor-sg"

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 9090
    to_port     = 9090
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

output "instance_ip" {
  value = aws_instance.monitor_target.public_ip
}
